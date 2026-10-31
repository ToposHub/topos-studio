"""
Timeline Clip Frame Provider - P3 Comp Clip integration.

为 Composite 提供 clip 级别的帧数据。
负责：timeline_frame -> clip_local_frame -> source_frame 映射
      + ColorStack 应用
      + 返回 GPU texture 给 Composite

第一阶段实现：
- 只支持当前活动时间线
- 只支持单个 clip 主输入
- 支持 input_stage="after_color"
- 回退链: ColorOutput 缓存 -> 源解码帧 -> 调试占位符

设计原则：
- 不直接创建视频解码器
- 不直接打开文件
- 通过项目结构和现有 GPU 管线获取帧数据
"""

from __future__ import annotations

import logging
import threading
import numpy as np
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.features.color.core.color_output import ColorOutput
    from src.features.color.core.image_engine import ImageEngine


@dataclass
class _TexResult:
    """Lightweight texture result for internal use.

    Avoids importing TextureResult from composite.nodes.base_node which
    would trigger the nodes package __init__.py and pull in moderngl.
    Converted to the real TextureResult by TimelineClipInputNode when
    the result enters the composite pipeline.

    Attributes mirror the public fields of
    src.features.composite.nodes.base_node.TextureResult.

    Origin convention:
        GPU texture storage: OpenGL origin, bottom-left.
        CPU image arrays: UI/image origin, top-left.
        Renderer FBO readback: returns CPU top-left BGR float32.
        Viewer local upload: accepts CPU top-left BGR, flips to GL bottom-left on upload.
        Flip only happens at boundaries: CPU->GL upload or GL->CPU readback, never in multiple places.
    """
    texture_id: int = 0
    width: int = 0
    height: int = 0
    revision: int = 0
    format: Any = None
    bbox: Any = None
    alpha_premultiplied: bool | None = None
    color_space: str | None = None
    bit_depth: int = 8
    frame_metadata: Any = None
    depth_texture_id: int | None = None
    has_deep_data: bool = False
    origin: str = "gl_bottom_left"

    @property
    def is_valid(self) -> bool:
        return self.texture_id > 0


# FX-M01: map a base texture's internal-format string (as carried by
# ``_TexResult.format``, e.g. ``"rgba16f"`` / ``"rgba32f"`` / ``"rgba8"``) to
# the ModernGL dtype the EffectPassRunner should allocate for its scratch
# surfaces. Float formats keep HDR headroom (no 8-bit clamp/banding); anything
# else falls back to 8-bit ``f1`` (the Edit preview path, which is 8-bit end
# to end). ``None`` / unknown formats default to ``f1`` so behaviour is
# unchanged for callers that don't advertise a format.
_FLOAT_FORMAT_TOKENS = ("16f", "32f", "16F", "32F")


def _dtype_for_tex_format(tex_format: Any) -> str:
    """Return the ModernGL scratch dtype matching ``tex_format``.

    ``tex_format`` is the tuple/string stored on a ``_TexResult`` (its first
    element is the GL internal-format name, e.g. ``"rgba16f"``). Returns
    ``"f2"`` (RGBA16F) for any float/HDR format and ``"f1"`` (8-bit)
    otherwise, including when the format is absent or unparseable.
    """
    if tex_format is None:
        return "f1"
    fmt = tex_format[0] if isinstance(tex_format, (tuple, list)) else tex_format
    try:
        fmt_str = str(fmt)
    except Exception:  # noqa: BLE001
        return "f1"
    if any(tok in fmt_str for tok in _FLOAT_FORMAT_TOKENS):
        return "f2"
    return "f1"


def _to_texture_result(tex_result: _TexResult) -> Any:
    """Convert internal _TexResult to the real TextureResult at the boundary.

    This is called by TimelineClipInputNode before the result enters the
    composite pipeline. If the import fails (no moderngl), returns the
    _TexResult as-is since it's duck-type compatible.
    """
    try:
        from src.features.composite.nodes.base_node import TextureResult
        return TextureResult(
            texture_id=tex_result.texture_id,
            width=tex_result.width,
            height=tex_result.height,
            revision=tex_result.revision,
            format=tex_result.format,
            bbox=tex_result.bbox,
            alpha_premultiplied=tex_result.alpha_premultiplied,
            color_space=tex_result.color_space,
            bit_depth=tex_result.bit_depth,
            frame_metadata=tex_result.frame_metadata,
            depth_texture_id=tex_result.depth_texture_id,
            has_deep_data=tex_result.has_deep_data,
        )
    except ImportError:
        return tex_result


class TimelineClipFrameProvider:
    """
    为 Composite 提供 timeline clip 的帧数据。

    不直接创建视频解码器。
    不直接打开文件。
    通过项目结构和现有 GPU 管线获取帧数据。

    回退链优先级：
    1. ColorOutput 缓存的调色后 GPU texture (after_color)
    2. 源视频解码帧上传为 GPU texture (source_raw / fallback)
    3. 调试占位符 texture (深灰底 + 红色横条)
    """

    def __init__(self) -> None:
        self._project = None
        self._timeline_sequences: dict[str, Any] = {}
        self._color_stack_textures: dict[str, Any] = {}  # {stack_id: texture_info}
        self._engine: ImageEngine | None = None
        self._gl_context: Any | None = None
        self._owned_textures: dict[int, Any] = {}
        self._last_source_texture_id: int | None = None
        self._debug_placeholder_cache: dict[tuple[int, int], Any] = {}
        self._yuv_uploader: Any | None = None
        self._source_texture_cache: dict[tuple[str, int], _TexResult] = {}
        self._source_cache_order: list[tuple[str, int]] = []
        self._SOURCE_CACHE_MAX = 3
        # AUD-18321: source textures evicted/replaced while a returned
        # ``_TexResult`` may still be sampled (composite graph evaluating
        # several source inputs before the final draw).  Eviction retires
        # the GL name here instead of releasing immediately; the retired
        # names are flushed (released) at the next render-thread entry,
        # i.e. after the previous evaluation's draw commands were issued
        # on this same GL thread.
        self._source_texture_retired: list[int] = []
        # Cross-thread clear support: the UI thread (asset switch in the
        # Composite page) must not release ModernGL textures off the render
        # thread, and must not mutate the cache/order pair concurrently with
        # the render thread's LRU bookkeeping.  A clear requested from a
        # non-GL thread parks on this flag and is drained at the next render
        # thread entry (get_timeline_clip_texture).
        self._gl_thread_id: int | None = None
        self._source_cache_clear_pending = False
        # AUD-18325: clear_debug_cache 从非 GL 线程调用时的停靠标志
        self._debug_cache_clear_pending = False
        # Phase 4: lazy EffectPassRunner for the after_clip_effects stage.
        # Created on first use against the provider's GL context.
        self._effect_runner: Any | None = None
        self._allow_external_gpu_frames = True
        # macOS VT frames are returned by the shared decode service as a
        # deferred CVPixelBuffer.  These resources are created lazily on the
        # Composite render thread, never on the decode worker.
        self._vt_import_bridge: Any | None = None
        self._external_yuv_pass: Any | None = None
        # (GLsync, bridge_release, gpu_frame_release).  A source IOSurface
        # remains leased until the GPU has consumed the conversion pass.
        self._external_source_releases: list[tuple[Any, Any, Any]] = []
        self._external_gpu_import_count = 0
        self._external_gpu_import_failures = 0
        self._external_gpu_fallback_reason = ""
        self._external_gpu_unavailable = False
        # This provider owns one reference per source path.  Calling
        # VideoFrameProviderService.open_clip() for every video frame leaks
        # references and repeatedly perturbs the shared decode session.
        self._frame_provider_service: Any | None = None
        self._video_session_ids: dict[str, str] = {}
        # Assets that returned no proxy frame once — they go straight to
        # master decode afterwards instead of retrying proxy (and logging a
        # warning) on every single playback frame.
        self._proxy_unavailable_assets: set[str] = set()
        # ── 跳转异步解码（持帧预览）────────────────────────────────────
        # 渲染线程上随机跳转（scrub）的源帧解码改为后台执行：本求值返回该
        # 素材最近一张有效纹理（持帧），解码完成后经回调通知页面重求值；
        # 顺序播放（前向小步）保持同步解码（延迟最低）。
        self._frame_ready_callback: Any | None = None
        self._async_decode_thread: Any | None = None
        self._async_decode_wakeup = threading.Event()
        self._async_decode_stop = False
        # AUD-18328: 后台解码 worker 的世代号。stop 协议触发时递增——
        # 旧 worker 绑定创建时的世代，即便 stop 标志被新 worker 复位，
        # 旧 worker 也会在下一次世代检查退出，且绝不把已清空的 pending
        # 回写（否则 release 后 pending 重新出现陈旧条目）。
        self._async_decode_generation: int = 0
        # asset -> (vid_clip_id, frame, matrix_tag, range_tag)
        self._async_targets: dict[str, tuple[str, int, str, str]] = {}
        # (asset, frame) -> 后台已解码的 DecodedFrame（等渲染线程上传）
        self._pending_source_frames: dict[tuple[str, int], Any] = {}
        # asset -> 最近一次成功上屏的源帧号（顺序/跳转判定）
        self._asset_last_frame: dict[str, int] = {}
        # vid_clip_id -> 色彩标签（会话内不变，避免渲染线程重复探测）
        self._clip_color_tags: dict[str, tuple[str, str, str, str]] = {}
        self._async_lock = threading.Lock()

    def set_project(self, project: Any) -> None:
        """设置当前项目"""
        self._project = project

    def set_engine(self, engine: Any) -> None:
        """设置 ImageEngine 引用，用于访问 ColorOutput。"""
        self._engine = engine

    def set_gl_context(self, gl_context: Any) -> None:
        """设置 Composite 渲染线程当前 ModernGL context。"""
        self._gl_context = gl_context

    def set_allow_external_gpu_frames(self, enabled: bool) -> None:
        """Enable capability-gated deferred GPU frame import.

        The provider never samples a texture name from another context.  On
        macOS the supported path is CVPixelBuffer/IOSurface import performed
        on the current Composite GL thread; unsupported platforms, formats,
        or missing contexts fall back to the existing planar decode path.
        """
        self._allow_external_gpu_frames = bool(enabled)

    def _ensure_external_gpu_resources(self) -> tuple[Any, Any] | None:
        """Create the VT bridge and external YUV pass on the GL thread."""
        if self._gl_context is None:
            self._external_gpu_fallback_reason = "no_current_composite_gl_context"
            return None
        if self._external_gpu_unavailable:
            return None
        if self._vt_import_bridge is None:
            try:
                from src.shared.media.macos_vt.iosurface_gl_bridge import (
                    IOSurfaceGLBridge,
                    is_iosurface_bridge_implemented,
                )
                if not is_iosurface_bridge_implemented():
                    self._external_gpu_fallback_reason = "iosurface_gl_bridge_unavailable"
                    self._external_gpu_unavailable = True
                    return None
                bridge = IOSurfaceGLBridge(ring_size=4, max_ring_size=16)
                if not bridge.initialize(planes_per_frame=2):
                    self._external_gpu_fallback_reason = (
                        getattr(bridge, "last_fallback_reason", None)
                        or "iosurface_gl_bridge_init_failed"
                    )
                    try:
                        bridge.destroy()
                    except Exception:
                        pass
                    self._external_gpu_unavailable = True
                    return None
                self._vt_import_bridge = bridge
            except Exception as error:  # noqa: BLE001
                self._external_gpu_fallback_reason = f"iosurface_gl_bridge_error:{error}"
                self._external_gpu_unavailable = True
                return None
        if self._external_yuv_pass is None:
            try:
                from src.shared.video.external_yuv_renderer import ExternalYuvToRgbPass
                external_pass = ExternalYuvToRgbPass(self._gl_context)
                # Force shader/VAO creation here so a compile failure is a
                # capability failure before any decoder surface is retained.
                if not external_pass.initialize():
                    self._external_gpu_fallback_reason = "external_yuv_shader_init_failed"
                    external_pass.release()
                    self._external_gpu_unavailable = True
                    return None
                self._external_yuv_pass = external_pass
            except Exception as error:  # noqa: BLE001
                self._external_gpu_fallback_reason = f"external_yuv_pass_error:{error}"
                self._external_gpu_unavailable = True
                return None
        return self._vt_import_bridge, self._external_yuv_pass

    @staticmethod
    def _release_external_frame_pair(gpu_frame: Any, bridge_release: Any) -> None:
        """Release both leases exactly once after the source pass is safe."""
        try:
            if bridge_release is not None and hasattr(bridge_release, "release"):
                bridge_release.release()
        finally:
            release = getattr(gpu_frame, "release", None)
            if release is not None and hasattr(release, "release"):
                release.release()

    def _enqueue_external_source_release(
        self,
        gpu_frame: Any,
        bridge_release: Any,
    ) -> None:
        """Fence the external conversion before returning the CVPixelBuffer."""
        try:
            from OpenGL.GL import (
                GL_SYNC_GPU_COMMANDS_COMPLETE,
                glFenceSync,
                glFlush,
            )
            sync = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            if sync:
                glFlush()
                self._external_source_releases.append(
                    (sync, bridge_release, gpu_frame)
                )
                return
        except Exception as error:  # noqa: BLE001
            self._external_gpu_fallback_reason = f"external_gpu_fence_unavailable:{error}"

        # A driver without sync support is still safe, but this exceptional
        # path intentionally pays for completion rather than releasing a
        # CVPixelBuffer while the GPU could still sample its IOSurface.
        try:
            from OpenGL.GL import glFinish
            glFinish()
        except Exception:
            pass
        self._release_external_frame_pair(gpu_frame, bridge_release)

    def _drain_external_source_releases(self, wait: bool = False) -> None:
        """Release source leases whose GL conversion fence has completed."""
        if not self._external_source_releases:
            return
        try:
            from OpenGL.GL import (
                GL_ALREADY_SIGNALED,
                GL_CONDITION_SATISFIED,
                GL_TIMEOUT_EXPIRED,
                glClientWaitSync,
                glDeleteSync,
                glFinish,
            )
            # wait=True 是 fence 语义的同步 drain：调用方显式要求全部
            # 挂起的外部源转换完成（非热路径 present），glFinish 保证
            # 已排队 fence 的 GPU 工作全部落盘。
            if wait:
                glFinish()
            pending: list[tuple[Any, Any, Any]] = []
            for sync, bridge_release, gpu_frame in self._external_source_releases:
                status = (
                    GL_ALREADY_SIGNALED
                    if wait
                    else glClientWaitSync(sync, 0, 0)
                )
                if status in (GL_ALREADY_SIGNALED, GL_CONDITION_SATISFIED):
                    try:
                        glDeleteSync(sync)
                    except Exception:
                        pass
                    self._release_external_frame_pair(gpu_frame, bridge_release)
                elif status == GL_TIMEOUT_EXPIRED:
                    pending.append((sync, bridge_release, gpu_frame))
                else:
                    pending.append((sync, bridge_release, gpu_frame))
            self._external_source_releases = pending
        except Exception as error:  # noqa: BLE001
            self._external_gpu_fallback_reason = f"external_gpu_fence_poll_failed:{error}"
            if wait:
                for _, bridge_release, gpu_frame in self._external_source_releases:
                    self._release_external_frame_pair(gpu_frame, bridge_release)
                self._external_source_releases.clear()

    def _release_external_gpu_resources(self, wait: bool = False) -> None:
        """Release deferred source leases and GL import resources."""
        self._drain_external_source_releases(wait=wait)
        if self._external_source_releases and wait:
            # If a platform reports an unusual sync status after glFinish,
            # release only after the completion barrier, then delete handles.
            try:
                from OpenGL.GL import glDeleteSync
                for sync, bridge_release, gpu_frame in self._external_source_releases:
                    try:
                        glDeleteSync(sync)
                    except Exception:
                        pass
                    self._release_external_frame_pair(gpu_frame, bridge_release)
            finally:
                self._external_source_releases.clear()
        if self._external_yuv_pass is not None:
            try:
                self._external_yuv_pass.release()
            except Exception:
                pass
            self._external_yuv_pass = None
        if self._vt_import_bridge is not None:
            try:
                self._vt_import_bridge.destroy()
            except Exception:
                pass
            self._vt_import_bridge = None

    def _import_deferred_gpu_frame(
        self,
        gpu_frame: Any,
        *,
        asset_path: str,
        source_frame: int,
        matrix_tag: str,
        range_tag: str,
        cs_tag: str,
        source_meta: dict,
    ) -> _TexResult | None:
        """Import a deferred VT frame and convert it to a cached RGB texture."""
        vt_frame = getattr(gpu_frame, "deferred_vt_frame", None)
        if vt_frame is None:
            self._external_gpu_fallback_reason = "deferred_vt_frame_missing"
            return None
        resources = self._ensure_external_gpu_resources()
        if resources is None:
            return None
        # Reclaim completed source leases before acquiring another ring slot;
        # otherwise a long playback can grow the ring to its hard limit while
        # the GPU is already finished with earlier conversion passes.
        self._drain_external_source_releases()
        bridge, external_pass = resources
        bound = None
        output_tex = None
        output_fbo = None
        draw_submitted = False
        try:
            bound = bridge.bind_pixel_buffer(
                pixel_buffer=getattr(vt_frame, "pixel_buffer", None),
                frame_index=int(getattr(vt_frame, "frame_index", source_frame) or source_frame),
                pixel_format=str(getattr(vt_frame, "pixel_format", "nv12") or "nv12"),
                bit_depth=int(getattr(vt_frame, "bit_depth", 8) or 8),
            )
            if bound is None:
                self._external_gpu_fallback_reason = (
                    getattr(bridge, "last_fallback_reason", None)
                    or "iosurface_bind_failed"
                )
                return None
            planes, bridge_release = bound
            width = int(getattr(vt_frame, "width", 0) or 0)
            height = int(getattr(vt_frame, "height", 0) or 0)
            # AUD-18322：VT 帧位深统一取 vt_frame（与 bind_pixel_buffer 同源），
            # >8-bit 源的 RGB 转换目标用 f2（RGBA16F）——f1 会在 IDT 展开
            # 前把 10/12-bit 编码值量化到 8-bit（暗部 banding）。
            vt_bit_depth = self._coerce_bit_depth(
                getattr(vt_frame, "bit_depth", None), default=8)
            if width <= 0 or height <= 0:
                self._external_gpu_fallback_reason = "deferred_frame_dimensions_invalid"
                self._release_external_frame_pair(gpu_frame, bridge_release)
                bound = None
                return None

            import moderngl
            output_tex = self._gl_context.texture(
                (width, height), 4,
                dtype=self._source_rgb_output_dtype(vt_bit_depth))
            output_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            output_tex.repeat_x = False
            output_tex.repeat_y = False
            output_fbo = self._gl_context.framebuffer(color_attachments=[output_tex])
            draw_submitted = True
            if not external_pass.render(planes, gpu_frame, output_fbo, width, height):
                self._external_gpu_fallback_reason = "external_yuv_render_failed"
                return None
            output_fbo.release()
            output_fbo = None
            self._owned_textures[output_tex.glo] = output_tex
            result = _TexResult(
                texture_id=output_tex.glo,
                width=width,
                height=height,
                color_space=cs_tag,
                alpha_premultiplied=False,
                bit_depth=vt_bit_depth,
                frame_metadata={
                    "origin": "gl_bottom_left",
                    "zero_copy": True,
                    "decode_path": getattr(
                        gpu_frame, "decode_path", "macos_vt_iosurface_zero_copy"
                    ),
                    "external_gpu_import": "iosurface_gl",
                    **source_meta,
                },
            )
            self._enqueue_external_source_release(gpu_frame, bridge_release)
            bound = None
            self._cache_source_texture(asset_path, source_frame, result)
            self._external_gpu_import_count += 1
            self._asset_last_frame[asset_path] = source_frame
            return result
        except Exception as error:  # noqa: BLE001
            self._external_gpu_import_failures += 1
            self._external_gpu_fallback_reason = f"external_gpu_import_error:{error}"
            return None
        finally:
            if output_fbo is not None:
                try:
                    output_fbo.release()
                except Exception:
                    pass
            if bound is not None:
                _, bridge_release = bound
                if draw_submitted:
                    self._enqueue_external_source_release(gpu_frame, bridge_release)
                else:
                    self._release_external_frame_pair(gpu_frame, bridge_release)
            if output_tex is not None and output_tex.glo not in self._owned_textures:
                try:
                    output_tex.release()
                except Exception:
                    pass

    def _retire_source_texture(self, tex_id: int) -> None:
        """Retire an evicted source texture for deferred release (AUD-18321).

        The cache no longer references the name, but a ``_TexResult``
        returned earlier in the current composite evaluation may still
        sample it before the final draw.  Actual release happens on the
        next render-thread entry (``_flush_retired_source_textures``).
        """
        self._source_texture_retired.append(tex_id)

    def _flush_retired_source_textures(self) -> None:
        """Release textures retired during the previous evaluation.

        GL/render-thread only (same contract as the cache clear drain).
        The previous evaluation's draw commands were already issued on
        this thread, so the GL objects are no longer referenced by any
        new sampling work.
        """
        if not self._source_texture_retired:
            return
        retired, self._source_texture_retired = self._source_texture_retired, []
        for tex_id in retired:
            if tex_id in self._owned_textures:
                self._release_owned_texture(tex_id)

    def _cache_source_texture(
        self,
        asset_path: str,
        source_frame: int,
        result: _TexResult,
    ) -> _TexResult:
        """Cache an owned source texture and evict within the bounded budget."""
        cache_key = (asset_path, source_frame)
        stale = self._source_texture_cache.get(cache_key)
        if stale is not None and stale.texture_id != result.texture_id:
            if stale.texture_id in self._owned_textures:
                self._retire_source_texture(stale.texture_id)
        # Keep the order as a real LRU list.  Source-frame requests are not
        # strictly sequential (scrubbing and reverse playback are common), so
        # preserving an old entry's insertion position can evict a frame that
        # is still being reused and trigger repeated decode/upload work.
        self._touch_source_cache_key(cache_key)
        self._source_texture_cache[cache_key] = result
        while len(self._source_cache_order) > self._SOURCE_CACHE_MAX:
            old_key = self._source_cache_order.pop(0)
            old_result = self._source_texture_cache.pop(old_key, None)
            if old_result and old_result.texture_id in self._owned_textures:
                # AUD-18321: 淘汰不再立即释放——同一次复合求值中，更早返回
                # 的节点仍可能在最终合成 draw 前采样该纹理名；延迟到下一次
                # 渲染线程入口再释放。
                self._retire_source_texture(old_result.texture_id)
        return result

    def _touch_source_cache_key(self, cache_key: tuple[str, int]) -> None:
        """Mark a source texture cache key as most recently used.

        The helper also repairs the order list when a test or legacy caller
        populated ``_source_texture_cache`` directly.  Keeping that invariant
        in one place prevents cache hits from silently bypassing eviction
        accounting.
        """
        try:
            self._source_cache_order.remove(cache_key)
        except ValueError:
            pass
        self._source_cache_order.append(cache_key)

    def set_frame_ready_callback(self, callback: Any | None) -> None:
        """注册"异步源帧解码完成"回调（合成页注入 Qt 信号 emit）。

        回调在后台解码线程上执行，接收方需自行保证线程安全（Qt 信号
        emit 天然跨线程队列连接）。为 None 时跳转退回全同步解码。
        """
        self._frame_ready_callback = callback

    def set_timeline_sequences(self, sequences: dict[str, Any]) -> None:
        """设置时间线序列缓存"""
        self._timeline_sequences = sequences

    def get_clip_frame_mapping(
        self,
        timeline_id: str,
        clip_id: str,
        timeline_frame: int,
    ) -> dict[str, int] | None:
        """
        计算 timeline_frame -> clip_local_frame -> source_frame 映射。

        Args:
            timeline_id: 时间线 ID
            clip_id: clip ID
            timeline_frame: 时间线帧号

        Returns:
            {"clip_local_frame": int, "source_frame": int} 或 None
        """
        timeline = self._timeline_sequences.get(timeline_id)
        if timeline is None:
            logger.warning(f"Timeline not found: {timeline_id}")
            return None

        clip = timeline.get_clip_by_id(clip_id)
        if clip is None:
            logger.warning(f"Clip not found: {clip_id}")
            return None

        try:
            clip_local_frame = timeline_frame - clip.timeline_start
            if clip_local_frame < 0 or clip_local_frame >= clip.timeline_duration:
                return None
            source_frame = clip.timeline_to_source_frame(timeline_frame)
            return {
                "clip_local_frame": clip_local_frame,
                "source_frame": source_frame,
            }
        except (ValueError, IndexError, AttributeError) as e:
            logger.warning(f"Frame mapping error: {e}")
            return None

    # ------------------------------------------------------------------
    # Main texture retrieval
    # ------------------------------------------------------------------

    def get_timeline_clip_texture(
        self,
        runtime_context: Any,
    ) -> Any | None:
        """
        获取 timeline clip 的 GPU texture。

        回退链：
        1. input_stage="after_color" 时，尝试 ColorOutput 缓存纹理
        2. 源视频解码帧上传 GPU texture
        3. 调试占位符 texture

        Args:
            runtime_context: CompositeRuntimeContext

        Returns:
            _TexResult (duck-type compatible with TextureResult) 或 None
        """
        if runtime_context is None:
            return None

        # This method is the provider's GL/render-thread entry: pin the
        # thread identity for the cross-thread clear contract, then execute
        # any clear parked by the UI thread before touching the cache.
        current_thread_id = threading.get_ident()
        if self._gl_thread_id is None:
            self._gl_thread_id = current_thread_id
        elif self._gl_thread_id != current_thread_id:
            # AUD-18323: provider 被第二个 GL 线程复用（如 facade 工厂与
            # CompositeFrameRenderer 先后驱动同一实例）。此前无条件覆写
            # pin：线程身份翻转后旧 GL 线程的 clear_source_cache 会被停靠
            # 给新线程代为释放（跨上下文释放纹理），两线程还可并发进入
            # 无锁的 cache/order 变更路径。视为上下文更换——作废全部
            # GL 记账后再重新绑定线程身份（旧上下文的 owned 纹理无法在
            # 新线程安全释放，只丢弃引用，等待旧 context 销毁时回收）。
            logger.warning(
                "TimelineClipFrameProvider: GL thread changed "
                f"({self._gl_thread_id} -> {current_thread_id}); "
                "invalidating GL-bound caches before re-pinning "
                "(single-GL-thread contract, AUD-18323)"
            )
            self._invalidate_gl_thread_state(current_thread_id)
        self._drain_pending_source_cache_clear()
        # AUD-18321: release source textures retired during the previous
        # evaluation — its draw commands are already queued on this thread.
        self._flush_retired_source_textures()

        logger.debug(
            f"TimelineClipFrameProvider.get_timeline_clip_texture: "
            f"graph={runtime_context.graph_id[:8] if runtime_context.graph_id else 'N/A'}, "
            f"frame={runtime_context.timeline_frame}, "
            f"input_stage={runtime_context.input_stage}"
        )

        input_stage = getattr(runtime_context, 'input_stage', 'after_color')
        width = getattr(runtime_context, 'width', 1920) or 1920
        height = getattr(runtime_context, 'height', 1080) or 1080

        # ── 1. ColorOutput cached texture (after_color) ──────────────
        if input_stage == "after_color":
            color_tex = self._try_color_output_texture(runtime_context)
            if color_tex is not None:
                return color_tex

        # ── 1b. after_clip_effects: after_color texture run through the
        # clip's video-filter chain. Falls back to after_color (and then
        # to the source path below) if there are no effects or the runner
        # is unavailable. (Phase 4.)
        if input_stage == "after_clip_effects":
            base_tex = self._try_color_output_texture(runtime_context)
            effects = getattr(runtime_context, 'clip_effects', ()) or ()
            if base_tex is not None and effects:
                processed = self._apply_after_clip_effects(
                    base_tex, effects, width, height, runtime_context)
                if processed is not None:
                    return processed
            # No effects or processing failed — return the base texture
            # (or fall through to the source path if base was None).
            if base_tex is not None:
                return base_tex

        # ── 2. Source raw decoded frame ─────────────────────────────
        source_tex = self._try_source_frame_texture(runtime_context)
        if source_tex is not None:
            return source_tex

        # Missing timeline media should not be presented as a valid frame in
        # user-facing Composite preview. Returning the debug placeholder here
        # makes the first page switch display a colored block that can then be
        # cached/replayed as if it were real output.
        logger.warning(
            "TimelineClipFrameProvider: no timeline clip texture available; "
            f"timeline={getattr(runtime_context, 'timeline_id', None) or 'N/A'} "
            f"clip={getattr(runtime_context, 'clip_id', None) or 'N/A'} "
            f"asset={getattr(runtime_context, 'asset_id', None) or 'N/A'} "
            f"frame={getattr(runtime_context, 'source_frame', 0)}"
        )
        return None

    # ------------------------------------------------------------------
    # Phase 4: after_clip_effects — run the clip's video-filter chain
    # on the after_color texture. Uses a lazy EffectPassRunner bound to
    # this provider's GL context (EffectPassRunner is "one runner per
    # context"; the provider's context is the Composite page's context,
    # separate from the Edit page's).
    # ------------------------------------------------------------------

    def _get_effect_runner(self) -> Any | None:
        """Lazily create an EffectPassRunner on this provider's context.

        Returns None if there's no GL context or shader init fails — the
        caller treats None as "skip effects" so Composite preview keeps
        working (just without clip filters).
        """
        if self._effect_runner is not None:
            return self._effect_runner
        if self._gl_context is None:
            return None
        try:
            from src.features.shared.effects.effect_pass_runner import EffectPassRunner
            self._effect_runner = EffectPassRunner(self._gl_context)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TimelineClipFrameProvider: EffectPassRunner init failed "
                "(%s); after_clip_effects stage will passthrough", exc,
            )
            self._effect_runner = None
        return self._effect_runner

    def _apply_after_clip_effects(
        self,
        base_tex: Any,
        effects: tuple,
        width: int,
        height: int,
        runtime_context: Any,
    ) -> Any | None:
        """Run ``effects`` on ``base_tex`` and return the processed texture.

        Mirrors EditGpuPreviewRenderer._apply_clip_effects: iterate by
        ``order``, skip disabled effects, resolve params at the current
        frame (keyframe-aware), call runner.run(). The returned glo is
        wrapped in a _TexResult carrying the same metadata as base_tex.

        Returns None when the runner is unavailable or every effect was
        skipped, so the caller can fall back to the unfiltered texture.
        """
        runner = self._get_effect_runner()
        if runner is None:
            return None

        from src.features.shared.effects import get_effect_catalog
        catalog = get_effect_catalog()

        # base_tex is a _TexResult (duck-typed TextureResult); the runner
        # wants a raw glo.
        input_glo = getattr(base_tex, "texture_id", None) or getattr(
            base_tex, "glo", None)
        if input_glo is None:
            return None

        # FX-M01: derive the scratch precision from base_tex's format so the
        # effect chain keeps HDR headroom instead of clamping to 8-bit. The
        # Composite page feeds RGBA16F base textures here; mapping that to
        # dtype="f2" means a HDR gradient survives Blur/Glow/Sharpen without
        # the 8-bit quantisation banding the old hard-coded "f1" scratch
        # produced. Falls back to "f1" for any non-float format.
        tex_format = getattr(base_tex, "format", None)
        scratch_dtype = _dtype_for_tex_format(tex_format)

        # FX-C05: effect animation lives in *global timeline* space.
        # ``source_frame`` is only for decoding the clip's media and can
        # diverge after trims, retimes, or a non-zero timeline start.  The
        # Edit CPU/GPU preview and export paths all use ``timeline_frame``;
        # Composite's ``after_clip_effects`` stage must use the exact same
        # coordinate or a comp-bound clip animates its effects at a different
        # time from the Edit page.  Keep source_frame only as a compatibility
        # fallback for legacy RuntimeContext-like callers that predate the
        # timeline_frame field.
        keyframe_frame = getattr(
            runtime_context,
            "timeline_frame",
            getattr(runtime_context, "source_frame", 0),
        )
        current_glo = input_glo
        ran_any = False
        for effect in sorted(effects, key=lambda e: getattr(e, "order", 0)):
            if not getattr(effect, "is_enabled", True):
                continue
            spec = catalog.get_spec(getattr(effect, "effect_type", ""))
            if spec is None:
                continue
            try:
                params = {
                    name: effect.get_param_at_frame(name, keyframe_frame)
                    for name in effect.params
                }
            except Exception:  # noqa: BLE001
                continue
            current_glo = runner.run(
                spec, current_glo, params, width, height, dtype=scratch_dtype)
            ran_any = True

        if not ran_any:
            return None

        # Wrap the result glo in a _TexResult so downstream composite
        # nodes can treat it uniformly. We reuse base_tex's metadata
        # (format/color_space/...) since the filter doesn't change those.
        return _TexResult(
            texture_id=current_glo,
            width=width,
            height=height,
            format=getattr(base_tex, "format", ("rgba16f", 4)),
            alpha_premultiplied=getattr(base_tex, "alpha_premultiplied", True),
            color_space=getattr(base_tex, "color_space", None),
            bbox=getattr(base_tex, "bbox", None),
        )

    # ------------------------------------------------------------------
    # Fallback 1: ColorOutput cached texture
    # ------------------------------------------------------------------

    def _try_color_output_texture(self, runtime_context: Any) -> _TexResult | None:
        """
        尝试从 ColorOutput 获取已调色的 GPU texture。

        仅在 engine 有有效 ColorOutput 时返回。
        """
        engine = self._engine
        if engine is None:
            return None

        color_output: ColorOutput | None = getattr(engine, 'color_output', None)
        if color_output is None:
            return None

        texture_id = color_output.texture_id
        if texture_id is None or texture_id <= 0:
            return None

        logger.debug(
            f"TimelineClipFrameProvider: using ColorOutput texture "
            f"tex_id={texture_id}, {color_output.width}x{color_output.height}"
        )

        return _TexResult(
            texture_id=texture_id,
            width=color_output.width,
            height=color_output.height,
            revision=color_output.revision,
            # The ColorOutput texture is the *graded* result from the Color
            # page's display pipeline, which renders to an sRGB-encoded target
            # (display-referred), regardless of the original source's
            # Rec.709/PQ/HLG transfer. So "srgb" is correct HERE — this is NOT
            # the raw source video (that path is _try_source_frame_texture,
            # which reads metadata and tags Rec.709/PQ/HLG appropriately).
            color_space="srgb",
            alpha_premultiplied=False,
        )

    # ------------------------------------------------------------------
    # Fallback 2: Source raw decoded frame
    # ------------------------------------------------------------------

    def _try_source_frame_texture(self, runtime_context: Any) -> _TexResult | None:
        """
        尝试通过 VideoFrameProviderService 获取源帧并上传为 GPU texture。

        需要 runtime_context 中有 clip_id 和 source_frame 信息。
        """
        clip_id = getattr(runtime_context, 'clip_id', None)
        if not clip_id:
            logger.warning("TimelineClipFrameProvider: source frame unavailable, missing clip_id")
            return None

        source_frame = getattr(runtime_context, 'source_frame', 0)

        try:
            from src.shared.video.frame_provider_service import get_frame_provider_service
            service = get_frame_provider_service()
        except (ImportError, RuntimeError):
            logger.warning("TimelineClipFrameProvider: VideoFrameProviderService not available")
            return None

        # Find the video path for this clip via project data
        asset_path = self._resolve_asset_path(runtime_context)
        if not asset_path:
            logger.warning(
                "TimelineClipFrameProvider: source frame unavailable, asset path unresolved "
                f"timeline={getattr(runtime_context, 'timeline_id', None) or 'N/A'} "
                f"clip={clip_id} asset={getattr(runtime_context, 'asset_id', None) or 'N/A'} "
                f"graph={getattr(runtime_context, 'graph_id', None) or 'N/A'}"
            )
            return None

        # Check source texture cache before decoding
        cache_key = (asset_path, source_frame)
        if cache_key in self._source_texture_cache:
            cached = self._source_texture_cache[cache_key]
            if cached.is_valid:
                self._touch_source_cache_key(cache_key)
                logger.debug(f"[TIMELINE-CLIP-FRAME] cache hit asset={asset_path} frame={source_frame}")
                return cached

        try:
            vid_clip_id = self._get_or_open_video_session(service, asset_path)
        except Exception as e:
            logger.warning(
                "TimelineClipFrameProvider: failed to open source session "
                f"path={asset_path}, error={e}"
            )
            return None

        # P0-4: detect the source video's transfer/gamut/matrix/range from clip
        # metadata so the Composite IDT decodes it correctly (Rec.709 video vs
        # sRGB still, and PQ/HLG for HDR) AND the GPU YUV uploader uses the
        # right YUV->RGB matrix + signal range. Previously every source frame
        # was tagged "srgb" with a default BT.709+limited upload, which made the
        # IDT apply the wrong transfer, silently dropped HDR, and decoded
        # BT.2020 HDR / full-range / BT.601 SD sources through the wrong matrix.
        source_cs_tag, source_prim_tag, source_matrix_tag, source_range_tag = (
            self._detect_source_color_space(service, vid_clip_id)
        )
        source_frame_meta = {
            "color_primaries": source_prim_tag,
            "color_transfer": source_cs_tag,
            "color_matrix": source_matrix_tag,
            "color_range": source_range_tag,
        }

        # ── Batch J：后台已解码的跳转帧优先上屏（渲染线程只做上传）──
        pending = self._pending_source_frames.pop((asset_path, source_frame), None)
        if pending is not None:
            pending_frame, pending_matrix_tag, pending_range_tag = pending
            result = self._finish_source_frame_upload(
                asset_path, source_frame, pending_frame,
                matrix_tag=pending_matrix_tag, range_tag=pending_range_tag,
                cs_tag=source_cs_tag, source_meta=source_frame_meta,
            )
            if result is not None:
                return result
            # 上传失败（如格式不识别）→ 继续走同步解码兜底

        # ── Batch J：随机跳转（scrub）→ 持帧 + 后台解码 ──────────────
        # 顺序播放（前向 ≤4 帧小步）保持同步：解码 ~15ms，同步延迟最低。
        # 跳转/倒退的 GOP 重解可达数百毫秒，交给后台线程，本求值先返回
        # 该素材最近一张有效纹理。
        last_frame = self._asset_last_frame.get(asset_path)
        is_sequential = last_frame is not None and 0 < source_frame - last_frame <= 4
        if not is_sequential and self._frame_ready_callback is not None:
            hold = self._hold_texture_for_asset(asset_path)
            if hold is not None:
                self._schedule_async_source_decode(
                    vid_clip_id, asset_path, source_frame,
                    source_matrix_tag, source_range_tag,
                )
                return hold
            # 没有可持纹理（该素材首帧）：退化为同步解码

        # Try a deferred GPU frame before CPU decode.  The service's VT path
        # deliberately returns a CVPixelBuffer wrapper with no texture name;
        # import and YUV conversion must happen on this Composite GL thread.
        if self._allow_external_gpu_frames:
            try:
                from src.shared.media.gpu_frame import (
                    is_gpu_decoded_frame,
                    safe_release_gpu_frame,
                )
                if hasattr(service, 'request_gpu_frame'):
                    gpu_frame = service.request_gpu_frame(vid_clip_id, source_frame, quality='proxy')
                    if (
                        gpu_frame is not None
                        and is_gpu_decoded_frame(gpu_frame)
                        and getattr(gpu_frame, "deferred_import", None)
                        == "macos_vt_pixel_buffer"
                    ):
                        result = self._import_deferred_gpu_frame(
                            gpu_frame,
                            asset_path=asset_path,
                            source_frame=source_frame,
                            matrix_tag=source_matrix_tag,
                            range_tag=source_range_tag,
                            cs_tag=source_cs_tag,
                            source_meta=source_frame_meta,
                        )
                        if result is not None:
                            logger.info(
                                "[TIMELINE-CLIP-FRAME] external GPU import: "
                                f"path={getattr(gpu_frame, 'decode_path', 'unknown')} "
                                f"asset={asset_path} frame={source_frame}"
                            )
                            return result
                        # Import failure is a bounded, explicit fallback.  The
                        # deferred frame owns a CVPixelBuffer reference even
                        # when bridge/shader setup fails.
                        safe_release_gpu_frame(gpu_frame)
                    elif gpu_frame is not None and is_gpu_decoded_frame(gpu_frame):
                        # A texture name from a different producer context is
                        # never sampled here.  Release it and continue through
                        # the ordinary CPU/planar path.
                        safe_release_gpu_frame(gpu_frame)
            except Exception as e:
                logger.debug(
                    f"Deferred GPU frame request/import failed: {e}, "
                    "falling back to CPU"
                )

        proxy_known_missing = asset_path in self._proxy_unavailable_assets
        try:
            # Playback hot path: never silently fall back to a full-res master
            # decode (blocks the render thread); the explicit master retry
            # below keeps first-frame correctness when proxy is unavailable.
            frame = None if proxy_known_missing else service.request_frame(
                vid_clip_id, source_frame, quality='proxy',
                allow_master_fallback=False,
            )
        except Exception as e:
            logger.warning(
                "TimelineClipFrameProvider: source frame decode failed "
                f"path={asset_path}, frame={source_frame}, error={e}"
            )
            return None

        if frame is None:
            if not proxy_known_missing:
                # Media without a proxy keeps returning None here. Remember
                # that per asset so subsequent frames skip the doomed proxy
                # request instead of emitting one warning per frame.
                self._proxy_unavailable_assets.add(asset_path)
                logger.info(
                    "TimelineClipFrameProvider: no proxy available for "
                    f"path={asset_path}; playing back from master"
                )
            try:
                frame = service.request_frame(vid_clip_id, source_frame, quality='master')
            except Exception as e:
                logger.warning(
                    "TimelineClipFrameProvider: master source frame decode failed "
                    f"path={asset_path}, frame={source_frame}, error={e}"
                )
                return None
            if frame is None:
                logger.warning(
                    "TimelineClipFrameProvider: source frame request returned None "
                    f"path={asset_path}, frame={source_frame}, qualities=proxy,master"
                )
                return None

        return self._finish_source_frame_upload(
            asset_path, source_frame, frame,
            matrix_tag=source_matrix_tag, range_tag=source_range_tag,
            cs_tag=source_cs_tag, source_meta=source_frame_meta,
        )

    def _finish_source_frame_upload(
        self,
        asset_path: str,
        source_frame: int,
        frame: Any,
        *,
        matrix_tag: str,
        range_tag: str,
        cs_tag: str,
        source_meta: dict,
    ) -> _TexResult | None:
        """把解码帧上传为 GPU 纹理并写入缓存（渲染线程调用）。

        同步路径与 Batch J 的后台 pending 路径共用：上传 + P0-4 色彩标记 +
        纹理缓存管理 + 顺序游标推进。
        """
        # Upload decoded frame to GPU texture. Pass the source matrix/range so
        # the planar YUV upload path configures the YUVGPUUploader correctly
        # (BT.2020 HDR / full-range / BT.601 SD instead of the BT.709+limited
        # default) before the YUV->RGB conversion runs.
        result = self._upload_decoded_frame(
            frame, matrix_tag=matrix_tag, range_tag=range_tag,
            cs_tag=cs_tag,
        )
        if result is None:
            logger.warning(
                "TimelineClipFrameProvider: decoded frame GPU upload failed "
                f"path={asset_path}, frame={source_frame}, "
                f"pixel_format={getattr(frame, 'pixel_format', 'unknown')}, "
                f"is_packed={getattr(frame, 'is_packed', None)}, "
                f"is_planar={getattr(frame, 'is_planar', None)}"
            )
            return None

        # P0-4: stamp the detected source color space + primaries onto the
        # uploaded texture so the Composite IDT decodes it correctly
        # (Rec.709/PQ/HLG instead of a hardcoded "srgb").
        result.color_space = cs_tag
        meta = dict(result.frame_metadata or {})
        meta.update(source_meta)
        result.frame_metadata = meta
        # Cache management — the cache owns the texture lifetime: release
        # happens only on eviction / replacement / explicit clear, never on
        # upload, so cached entries can never dangle.
        self._cache_source_texture(asset_path, source_frame, result)
        # 顺序游标：供跳转/顺序判定使用
        self._asset_last_frame[asset_path] = source_frame
        return result

    def _hold_texture_for_asset(self, asset_path: str) -> _TexResult | None:
        """返回该素材最近一张仍有效的缓存纹理（跳转持帧用）。

        纹理仍在 _source_texture_cache 中被正常持有，直接复用不影响其
        生命周期（淘汰/替换逻辑照常）。
        """
        for key in reversed(self._source_cache_order):
            if key[0] != asset_path:
                continue
            cached = self._source_texture_cache.get(key)
            if cached is not None and cached.is_valid:
                return cached
        return None

    def _schedule_async_source_decode(
        self,
        vid_clip_id: str,
        asset_path: str,
        source_frame: int,
        matrix_tag: str,
        range_tag: str,
    ) -> None:
        """登记一个跳转帧的后台解码目标（latest-wins per asset）并唤醒 worker。"""
        with self._async_lock:
            self._async_targets[asset_path] = (
                vid_clip_id, int(source_frame), matrix_tag, range_tag,
            )
        self._ensure_async_decode_thread()
        self._async_decode_wakeup.set()

    def _ensure_async_decode_thread(self) -> None:
        thread = self._async_decode_thread
        if thread is not None and thread.is_alive():
            return
        self._async_decode_stop = False
        self._async_decode_wakeup.clear()
        # AUD-18328: 新 worker 绑定当前世代号；被弃引用（join=False）的
        # 旧 worker 持有更小世代，复位 stop 标志不会"复活"它。
        generation = self._async_decode_generation
        thread = threading.Thread(
            target=self._async_decode_worker,
            args=(generation,),
            name="TimelineClipAsyncDecode",
            daemon=True,
        )
        self._async_decode_thread = thread
        thread.start()

    def _async_decode_worker(self, generation: int) -> None:
        """后台解码跳转帧：CPU 解码（服务内部有会话锁，线程安全）。

        AUD-18328: worker 绑定创建时的世代号。``release_video_sessions``
        停止协议（join=False 弃引用）会递增世代号——旧 worker 即使看到
        stop 标志被复位，也会在世代/停止检查处退出，并且不会把已清空的
        pending 回写。
        """
        while not self._async_decode_stop and (
            self._async_decode_generation == generation
        ):
            self._async_decode_wakeup.wait(timeout=0.5)
            self._async_decode_wakeup.clear()
            if self._async_decode_stop or (
                self._async_decode_generation != generation
            ):
                return
            with self._async_lock:
                targets = dict(self._async_targets)
                self._async_targets.clear()
            if not targets:
                continue
            from src.shared.video.frame_provider_service import get_frame_provider_service
            try:
                service = get_frame_provider_service()
            except (ImportError, RuntimeError):
                return
            for asset_path, (vid_clip_id, frame, matrix_tag, range_tag) in targets.items():
                if self._async_decode_stop or (
                    self._async_decode_generation != generation
                ):
                    return
                decoded = self._decode_source_frame_via_service(
                    service, vid_clip_id, frame, asset_path,
                )
                if decoded is None:
                    continue
                with self._async_lock:
                    if self._async_decode_generation != generation:
                        # AUD-18328: release_video_sessions 已清空 pending/
                        # 会话——过期世代不得把陈旧帧写回刚清空的缓存
                        return
                    # AUD-18320: latest-wins per asset。长距离 scrub 期间
                    # 每个浏览过的帧号都会在 pending 里滞留一张全尺寸解码帧
                    # （4K rgb48le ≈ 49MB/张），仅在精确同帧再次请求时才被
                    # 弹出——逐出该素材更早的 pending 条目，使每个素材最多
                    # 保留最新一张待上传帧。
                    stale_pending = [
                        key
                        for key in self._pending_source_frames
                        if key[0] == asset_path and key != (asset_path, frame)
                    ]
                    for stale_key in stale_pending:
                        self._pending_source_frames.pop(stale_key, None)
                    self._pending_source_frames[(asset_path, frame)] = (
                        decoded, matrix_tag, range_tag,
                    )
                callback = self._frame_ready_callback
                if callback is not None:
                    try:
                        callback()
                    except Exception as e:
                        logger.debug(f"TimelineClipFrameProvider: frame-ready callback failed: {e}")

    def _decode_source_frame_via_service(
        self, service: Any, vid_clip_id: str, source_frame: int, asset_path: str,
    ) -> Any | None:
        """在后台线程解码一帧（proxy 优先，master 兜底，与同步路径同策略）。"""
        proxy_known_missing = asset_path in self._proxy_unavailable_assets
        try:
            frame = None if proxy_known_missing else service.request_frame(
                vid_clip_id, source_frame, quality='proxy',
                allow_master_fallback=False,
            )
        except Exception as e:
            logger.debug(
                f"TimelineClipFrameProvider: async proxy decode failed "
                f"path={asset_path}, frame={source_frame}, error={e}"
            )
            return None
        if frame is None:
            if not proxy_known_missing:
                self._proxy_unavailable_assets.add(asset_path)
            try:
                frame = service.request_frame(vid_clip_id, source_frame, quality='master')
            except Exception as e:
                logger.debug(
                    f"TimelineClipFrameProvider: async master decode failed "
                    f"path={asset_path}, frame={source_frame}, error={e}"
                )
                return None
        return frame

    def _stop_async_decode_thread(self, join: bool = False, timeout: float = 1.0) -> None:
        """停止后台解码线程（换上下文/销毁时调用）。"""
        # AUD-18328: 递增世代号——join=False 弃引用后旧 worker 可能仍阻塞
        # 在解码里；世代号保证它随后自行退出（即使 _ensure 复位 stop 标志），
        # 且不会再把陈旧帧回写 pending。
        self._async_decode_generation += 1
        self._async_decode_stop = True
        self._async_decode_wakeup.set()
        thread = self._async_decode_thread
        if (
            join
            and thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=timeout)
        self._async_decode_thread = None

    def _get_or_open_video_session(self, service: Any, asset_path: str) -> str:
        """Return this provider's stable decode-session reference for a path."""
        if self._frame_provider_service is not service:
            self.release_video_sessions()
            self._frame_provider_service = service

        clip_id = self._video_session_ids.get(asset_path)
        if clip_id:
            has_clip = getattr(service, "has_clip", None)
            if not callable(has_clip) or has_clip(clip_id):
                return clip_id
            self._video_session_ids.pop(asset_path, None)

        clip_id = service.open_clip(asset_path)
        self._video_session_ids[asset_path] = clip_id
        return clip_id

    def _detect_source_color_space(self, service: Any, clip_id: str) -> tuple[str, str, str, str]:
        """Detect the source video's color tags from metadata.

        Returns ``(color_space_tag, primaries_tag, matrix_tag, range_tag)``:
          - color_space_tag: the transfer-function tag the Composite IDT
            understands ("srgb"/"bt709"/"pq"/"hlg"). Defaults to "bt709"
            (consumer video standard), NOT "srgb" — a timeline/clip source is
            video, so its default transfer is Rec.709, distinct from still-image
            sRGB. This is the P0-4 fix: previously this whole path hardcoded
            "srgb", so Rec.709 gamma and HDR (PQ/HLG) were lost at the IDT.
          - primaries_tag: the source gamut ("bt709"/"bt2020"/"smpte432") for
            the IDT gamut conversion; defaults to "bt709".
          - matrix_tag: the YUV->RGB matrix ("bt709"/"bt2020"/"bt601") used by
            the GPU YUV uploader. Defaults to "bt709" (HD norm). BT.2020 HDR
            sources are forced to "bt2020" regardless of metadata, since PQ/HLG
            are defined for BT.2020.
          - range_tag: the signal range ("limited"/"full"). Defaults to
            "limited" (broadcast/TV norm — the safe assumption for video).

        Falls back gracefully when the clip / metadata is unavailable.

        每个 clip 的标签在会话内不变（元数据级探测），按 vid_clip_id 记忆化，
        避免每次 miss 都在渲染线程重复构造 ClipInfo。
        """
        tags_cache = getattr(self, '_clip_color_tags', None)
        if tags_cache is None:
            # 轻量测试会用 __new__ 跳过 __init__；按需补建
            tags_cache = self._clip_color_tags = {}
        cached_tags = tags_cache.get(clip_id)
        if cached_tags is not None:
            return cached_tags
        try:
            info = service.get_clip_info(clip_id) if hasattr(service, "get_clip_info") else None
        except Exception as e:
            logger.debug(
                f"TimelineClipFrameProvider: clip info probe failed for "
                f"{clip_id}: {e}"
            )
            info = None
        if info is None:
            # AUD-18326: 默认标签同样按 clip_id 记忆化——此前早退不写缓存，
            # 无 clip info 的素材在渲染热路径上每帧重复探测 get_clip_info。
            default_tags = ("bt709", "bt709", "bt709", "limited")
            tags_cache[clip_id] = default_tags
            return default_tags

        transfer = (getattr(info, "color_transfer", "") or "").lower()
        primaries = (getattr(info, "color_primaries", "") or "").lower()
        matrix = (getattr(info, "color_matrix", "") or "").lower()
        # ClipInfo exposes color_matrix but not color_range; read it
        # best-effort (some callers pass a richer metadata object).
        range_ = (getattr(info, "color_range", "") or "").lower()

        # Map the FFmpeg transfer string to the IDT tag.
        cs_tag = "bt709"
        if transfer in ("smpte2084", "smpte2084-10"):
            cs_tag = "pq"
        elif transfer in ("arib-std-b67",):
            cs_tag = "hlg"
        elif transfer in ("bt709", "bt1886", "smpte170m", "smpte240m"):
            cs_tag = "bt709"
        elif transfer in ("traw-log0", "topos-traw-log0"):
            # RC7：TRAW LOG0（.toos/MOV RAW 源，transfer code 20）→
            # 显式标签（复合 u_transfer=5 PWL-log12 解码）；此前 unknown
            # 落默认 bt709（RAW 源被错套 BT.709 反 OETF）
            cs_tag = "traw-log0"
        elif transfer in ("linear",):
            # M1-F5：linear（FFmpeg code 8；.toos 浮点档 transfer=linear、
            # EXR linear 等场景线性源）→ IDT "linear" 标签——复合 IDT 对
            # linear 直接透传（数据已是线性，不施 BT.709 反 OETF；此前落
            # 默认 bt709 会给线性内容多套一条 BT.709 解码曲线 → 偏暗）。
            cs_tag = "linear"
        else:
            # M2-G1：toos 数字码防御兼容（IDSC CICP 码偶发以 int 透传——
            # 正常路径经 map_color_transfer 已是字符串）。TRAW_LOG0(20)
            # 随 RC7 统一词表，此处不猜测。
            try:
                transfer_code = int(transfer)
            except (TypeError, ValueError):
                transfer_code = None
            if transfer_code == 8:
                cs_tag = "linear"
            elif transfer_code == 16:
                cs_tag = "pq"
            elif transfer_code == 18:
                cs_tag = "hlg"
            elif transfer_code == 20:
                # RC7 词表统一（2026-09-21 复查补）：字符串路径已映射
                # traw-log0，数字码防御分支同步——否则同一源两种表示
                # 走出两种 IDT。
                cs_tag = "traw-log0"
        # else: unknown transfer → default bt709 (consumer-video assumption).

        # Map primaries to the gamut tag the IDT shader understands.
        prim_tag = "bt709"
        if primaries in ("bt2020", "bt2020nc", "bt2020_10", "bt2020_12"):
            prim_tag = "bt2020"
        elif primaries in ("smpte431", "smpte432"):
            prim_tag = "smpte432"
        # PQ/HLG transfers are defined for BT.2020 content.
        if cs_tag in ("pq", "hlg"):
            prim_tag = "bt2020"

        # Map the YUV matrix for the GPU uploader. FFmpeg matrix values:
        # bt709/smpte170m → BT.709, bt2020nc/bt2020c → BT.2020,
        # smpte240m/bt470bg → BT.601. HDR (PQ/HLG) is always BT.2020.
        matrix_tag = "bt709"
        if matrix in ("bt2020nc", "bt2020c", "bt2020"):
            matrix_tag = "bt2020"
        elif matrix in ("smpte240m", "bt470bg", "smpte170m", "bt601"):
            matrix_tag = "bt601"
        if cs_tag in ("pq", "hlg"):
            matrix_tag = "bt2020"

        # Range: "tv"/"limited" → limited; "pc"/"full"/"jpeg" → full.
        # Consumer video is limited-range by default.
        if range_ in ("pc", "full", "jpeg"):
            range_tag = "full"
        else:
            range_tag = "limited"

        tags = (cs_tag, prim_tag, matrix_tag, range_tag)
        tags_cache[clip_id] = tags
        return tags

    def _resolve_asset_path(self, runtime_context: Any) -> str | None:
        """
        从 runtime_context 和项目数据中解析素材路径。

        Returns:
            文件路径字符串，或 None
        """
        if self._project is None:
            return None

        asset_id = getattr(runtime_context, 'asset_id', None)
        if not asset_id:
            asset_id = self._resolve_clip_asset_id(runtime_context)
        if not asset_id:
            return self._resolve_composition_source_path(runtime_context)

        # Try to get path from project's asset registry. A promoted Comp Clip
        # replaces clip.asset_id with a virtual topos://comp_clip/... asset; the
        # timeline input still needs the original source media as its root plate.
        try:
            asset = self._project.get_asset_by_id(asset_id)
        except (AttributeError, TypeError):
            asset = None

        return self._resolve_media_asset_path(asset, asset_id, runtime_context)

    def _resolve_media_asset_path(
        self,
        asset: Any,
        asset_id: str,
        runtime_context: Any,
    ) -> str | None:
        """Resolve a real filesystem path from a MediaAsset or asset dict."""
        if not asset:
            return self._resolve_composition_source_path(runtime_context)

        if isinstance(asset, dict):
            media_type = asset.get("media_type", "")
            path = asset.get("path", "")
            metadata = asset.get("metadata") or {}
        else:
            media_type = getattr(asset, "media_type", "")
            path = getattr(asset, "path", "")
            metadata = getattr(asset, "metadata", {}) or {}

        is_comp_clip = media_type == "comp_clip" or str(path).startswith("topos://comp_clip/")
        if not is_comp_clip:
            return path or None

        # First unwrap through the Comp Clip asset metadata.
        original_asset_id = metadata.get("original_asset_id")
        original_path = self._resolve_original_asset_path(original_asset_id)
        if original_path:
            return original_path

        # Then use the composition graph metadata, which is the most stable
        # source once the graph has been promoted/saved.
        graph_id = (
            metadata.get("graph_id")
            or getattr(runtime_context, "graph_id", None)
            or getattr(runtime_context, "comp_clip_id", None)
        )
        source_path = self._resolve_composition_source_path(runtime_context, graph_id=graph_id)
        if source_path:
            return source_path

        # Finally scan CompClipData records by asset_id in case the runtime
        # context came from a stale timeline clip.
        for cc_data in getattr(self._project, "comp_clips", {}).values():
            if not isinstance(cc_data, dict):
                continue
            if cc_data.get("asset_id") != asset_id:
                continue
            original_path = self._resolve_original_asset_path(cc_data.get("original_asset_id"))
            if original_path:
                return original_path
            source_path = self._resolve_composition_source_path(
                runtime_context,
                graph_id=cc_data.get("graph_id"),
            )
            if source_path:
                return source_path

        logger.warning(
            "TimelineClipFrameProvider: comp clip source media unresolved "
            f"asset_id={asset_id}, graph_id={graph_id or 'N/A'}"
        )
        return None

    def _resolve_original_asset_path(self, asset_id: str | None) -> str | None:
        """Resolve a real path for an original source asset id."""
        if not asset_id or self._project is None:
            return None
        try:
            asset = self._project.get_asset_by_id(asset_id)
        except (AttributeError, TypeError):
            asset = None
        if isinstance(asset, dict):
            path = asset.get("path", "")
            media_type = asset.get("media_type", "")
        else:
            path = getattr(asset, "path", "")
            media_type = getattr(asset, "media_type", "")
        if path and media_type != "comp_clip" and not str(path).startswith("topos://comp_clip/"):
            return path
        return None

    def _resolve_composition_source_path(
        self,
        runtime_context: Any,
        graph_id: str | None = None,
    ) -> str | None:
        """Resolve source media from CompositeGraphData metadata."""
        if self._project is None:
            return None

        candidate_graph_ids = [
            graph_id,
            getattr(runtime_context, "graph_id", None),
            getattr(runtime_context, "comp_clip_id", None),
        ]
        for candidate in candidate_graph_ids:
            if not candidate:
                continue
            comp_data = None
            try:
                comp_data = self._project.get_composition_by_id(candidate)
            except (AttributeError, TypeError):
                comp_data = getattr(self._project, "compositions", {}).get(candidate)
            if not comp_data:
                continue
            if not isinstance(comp_data, dict):
                try:
                    comp_data = comp_data.to_dict()
                except AttributeError:
                    continue

            source_path = comp_data.get("source_asset_path")
            if source_path:
                return source_path

            source_refs = comp_data.get("source_refs") or {}
            source_path = source_refs.get("original_asset_path")
            if source_path:
                return source_path

            source_asset_id = (
                comp_data.get("source_asset_id")
                or source_refs.get("original_asset_id")
            )
            source_path = self._resolve_original_asset_path(source_asset_id)
            if source_path:
                return source_path

        return None

    def _resolve_clip_asset_id(self, runtime_context: Any) -> str | None:
        """从 timeline/clip 上下文回退解析素材 ID。"""
        clip_id = getattr(runtime_context, 'clip_id', None)
        if not clip_id:
            return None

        timeline_id = getattr(runtime_context, 'timeline_id', None)
        timeline = self._timeline_sequences.get(timeline_id) if timeline_id else None
        timelines = [timeline] if timeline is not None else list(self._timeline_sequences.values())

        for seq in timelines:
            try:
                clip = seq.get_clip_by_id(clip_id)
            except AttributeError:
                clip = None
            if clip is not None:
                return getattr(clip, 'asset_id', None)

        return None

    def _upload_decoded_frame(
        self, frame: Any, matrix_tag: str = "bt709", range_tag: str = "limited",
        cs_tag: str = "bt709",
    ) -> _TexResult | None:
        """
        将解码帧上传为 GPU texture，返回 _TexResult。

        需要 GL 上下文在线。此方法在 render thread 上执行，
        所以 GL 上下文应该是可用的。

        Args:
            frame: 解码帧（DecodedFrame）。
            matrix_tag: YUV->RGB matrix ("bt709"/"bt2020"/"bt601") from the
                source metadata — forwarded to the planar upload path so the
                YUVGPUUploader decodes YUV with the correct matrix.
            range_tag: signal range ("limited"/"full") from source metadata —
                forwarded so the uploader uses the correct range expansion.
            cs_tag: transfer-function tag ("srgb"/"bt709"/"pq"/"hlg"). The
                YUV→RGB step (GPU or CPU) only applies matrix + range, so the
                RGB result stays in the source's *encoded* transfer. Tagging
                it "srgb" unconditionally made the Composite IDT decode
                BT.709 video with the sRGB EOTF — a midtone shift (P0-COLOR-06).
        """
        try:
            import moderngl
            ctx = self._gl_context
            if ctx is None:
                return None

            # Get frame data as numpy array
            frame_data = None
            if hasattr(frame, 'data'):
                frame_data = frame.data
            elif hasattr(frame, 'image'):
                frame_data = frame.image
            elif hasattr(frame, 'numpy'):
                frame_data = frame.numpy()

            if frame_data is None:
                if getattr(frame, "is_planar", False) or getattr(frame, "planes", None):
                    return self._upload_planar_frame(
                        frame, matrix_tag=matrix_tag, range_tag=range_tag,
                        cs_tag=cs_tag,
                    )
                return None

            # Ensure numpy array and prepare it for a ModernGL texture upload.
            if not isinstance(frame_data, np.ndarray):
                return None

            prepared = self._prepare_packed_frame_upload(frame, frame_data)
            if prepared is None:
                return None

            upload_data, components, texture_dtype, bit_depth = prepared
            h, w = upload_data.shape[:2]
            tex = ctx.texture(
                (w, h),
                components,
                data=upload_data.tobytes(),
                dtype=texture_dtype,
            )

            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = False
            tex.repeat_y = False
            # NOTE: do NOT release the previous source texture here — it may
            # still live in _source_texture_cache; releasing it left dangling
            # GL names in the cache, so a repeated same-frame request hit a
            # freed/repurposed texture and flashed wrong color for 1-2 frames.
            # Texture lifetime is owned by the cache eviction path below.
            self._owned_textures[tex.glo] = tex
            self._last_source_texture_id = tex.glo

            return _TexResult(
                texture_id=tex.glo,
                width=tex.size[0],
                height=tex.size[1],
                # Packed RGB from a CPU YUV→RGB conversion keeps the source's
                # encoded transfer (the conversion is matrix+range only), so
                # tag the IDT with the source transfer instead of "srgb".
                color_space=cs_tag,
                alpha_premultiplied=False,
                bit_depth=bit_depth,
                frame_metadata={"origin": "gl_bottom_left"},
            )

        except Exception as e:
            logger.debug(f"Frame upload failed: {e}")
            return None

    def _prepare_packed_frame_upload(
        self,
        frame: Any,
        frame_data: np.ndarray,
    ) -> tuple[np.ndarray, int, str, int] | None:
        """Prepare packed RGB/BGR frames for texture upload.

        `VideoFrameProviderService` may return master frames as rgb48le
        packed uint16. ModernGL `dtype='f1'` only matches uint8 uploads, so
        high-bit-depth packed frames must be normalized to float before upload.

        Origin convention (CPU -> GPU boundary):
            CPU decoded frames use top-left origin (row 0 = top of image).
            OpenGL textures use bottom-left origin (row 0 = bottom of image).
            np.flipud is applied exactly once here at the CPU->GPU upload
            boundary to convert from top-left to bottom-left. No additional
            flips should occur inside the GPU pipeline.
        """
        if frame_data.ndim != 3:
            return None

        h, w, channels = frame_data.shape
        if h <= 0 or w <= 0 or channels not in (3, 4):
            return None

        pixel_format = str(getattr(frame, "pixel_format", "") or "").lower()
        bit_depth = self._coerce_bit_depth(getattr(frame, "bit_depth", None), default=8)
        is_bgr_order = pixel_format.startswith("bgr") or pixel_format.startswith("bgra")

        data = frame_data[..., :channels]

        if data.dtype == np.uint8:
            if is_bgr_order:
                if channels == 3:
                    data = data[..., ::-1]
                else:
                    data = data[..., [2, 1, 0, 3]]
            # CPU top-left -> GL bottom-left: flip once at upload boundary
            upload_data = np.ascontiguousarray(np.flipud(data))
            return upload_data, channels, "f1", bit_depth

        if data.dtype == np.uint16:
            max_value = self._packed_frame_normalization_max(frame, pixel_format, bit_depth)
            rgb = data[..., :3]
            if is_bgr_order:
                rgb = rgb[..., ::-1]

            upload_data = np.empty((h, w, 4), dtype=np.float32)
            upload_data[..., :3] = rgb.astype(np.float32) / max_value
            if channels == 4:
                upload_data[..., 3] = data[..., 3].astype(np.float32) / max_value
            else:
                upload_data[..., 3] = 1.0
            # CPU top-left -> GL bottom-left: flip once at upload boundary
            return np.ascontiguousarray(np.flipud(upload_data)), 4, "f4", bit_depth

        if data.dtype in (np.float16, np.float32):
            rgb = data[..., :3]
            if is_bgr_order:
                rgb = rgb[..., ::-1]

            upload_data = np.empty((h, w, 4), dtype=np.float32)
            upload_data[..., :3] = rgb.astype(np.float32)
            if channels == 4:
                upload_data[..., 3] = data[..., 3].astype(np.float32)
            else:
                upload_data[..., 3] = 1.0
            # CPU top-left -> GL bottom-left: flip once at upload boundary
            return np.ascontiguousarray(np.flipud(upload_data)), 4, "f4", bit_depth

        return None

    @staticmethod
    def _packed_frame_normalization_max(
        frame: Any,
        pixel_format: str,
        bit_depth: int,
    ) -> float:
        """Return normalization max for packed high-bit-depth frames."""
        packed_full_range_formats = {
            "rgb48",
            "rgb48le",
            "rgb48be",
            "rgba64",
            "rgba64le",
            "rgba64be",
            "bgr48",
            "bgr48le",
            "bgr48be",
            "bgra64",
            "bgra64le",
            "bgra64be",
        }
        if pixel_format in packed_full_range_formats:
            return 65535.0

        bit_depth = TimelineClipFrameProvider._coerce_bit_depth(
            getattr(frame, "bit_depth", None),
            default=bit_depth,
        )
        if bit_depth == 10:
            return 1023.0
        if bit_depth == 12:
            return 4095.0
        if bit_depth == 14:
            return 16383.0
        return 65535.0

    @staticmethod
    def _coerce_bit_depth(value: Any, default: int = 8) -> int:
        """Coerce frame bit-depth metadata without trusting mock/proxy objects."""
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            return default
        if coerced <= 0 or coerced > 64:
            return default
        return coerced

    @staticmethod
    def _source_rgb_output_dtype(bit_depth: int) -> str:
        """AUD-18322：源 YUV→RGB 转换渲染目标的 ModernGL dtype。

        >8-bit 源（P010/10-12bit YUV/GBR、VT 10-bit）用 ``f2``（RGBA16F）
        保留半精度——``f1`` 附件会在 IDT 展开前把高位深编码值量化到
        8-bit（暗部 banding）；8-bit 源维持 ``f1``（与 FX-M01 的 f2
        scratch 精度策略对齐）。
        """
        return "f2" if int(bit_depth) > 8 else "f1"

    def _upload_planar_frame(
        self, frame: Any, matrix_tag: str = "bt709", range_tag: str = "limited",
        cs_tag: str = "bt709",
    ) -> _TexResult | None:
        """Upload planar YUV/NV12/P010 frames and convert them to RGB on GPU.

        Args:
            frame: decoded planar frame (DecodedFrame with .planes).
            matrix_tag: YUV->RGB matrix ("bt709"/"bt2020"/"bt601") from source
                metadata. Applied to the YUVGPUUploader BEFORE the YUV->RGB
                render so BT.2020 HDR / BT.601 SD sources decode through the
                correct matrix (otherwise everything uses the BT.709 default
                and BT.2020/SD colors are wrong).
            range_tag: signal range ("limited"/"full") from source metadata.
                Applied so full-range (PC/JPEG) sources aren't double-compressed.
        """
        try:
            import moderngl

            ctx = self._gl_context
            if ctx is None:
                return None

            planes = getattr(frame, "planes", None)
            if not planes or len(planes) < 2:
                return None

            # Decoders expose planes in CPU top-left row order. The Composite
            # graph samples textures using bottom-left UV coordinates, so flip
            # each plane once at this CPU->GL boundary.
            upload_planes = self._prepare_planar_upload_planes(planes)

            if self._yuv_uploader is None:
                from src.shared.video.yuv_gpu_upload import YUVGPUUploader
                self._yuv_uploader = YUVGPUUploader(ctx, pool_size=3)

            width = int(getattr(frame, "width", 0) or planes[0].shape[1])
            height = int(getattr(frame, "height", 0) or planes[0].shape[0])
            pixel_format = str(getattr(frame, "pixel_format", "") or "").lower()
            frame_idx = int(getattr(frame, "frame_index", 0) or 0)

            bit_depth = 8
            plane_infos = getattr(frame, "plane_infos", None)
            if plane_infos:
                bit_depth = int(getattr(plane_infos[0], "bit_depth", bit_depth) or bit_depth)
            elif hasattr(frame, "bit_depth"):
                bit_depth = int(getattr(frame, "bit_depth") or bit_depth)

            # YUVA 第四平面（Topos / FFmpeg yuva*）：Alpha 位深独立于 luma。
            # upload_planes 已整体 flipud（_prepare_planar_upload_planes），alpha 同步。
            alpha_plane = None
            alpha_bit_depth = bit_depth
            alpha_premult = False
            if len(upload_planes) >= 4:
                candidate = upload_planes[3]
                if candidate.ndim == 2 and candidate.shape[:2] == (height, width):
                    alpha_plane = candidate
                    if plane_infos and len(plane_infos) >= 4:
                        alpha_bit_depth = int(
                            getattr(plane_infos[3], "bit_depth", bit_depth) or bit_depth)
                    alpha_premult = bool(
                        (getattr(frame, "extra", None) or {}).get(
                            "topos_alpha_premultiplied", False))

            # AUD-18322：>8-bit 平面源（P010/10-12bit YUV、GBR）在 YUV→RGB
            # 渲染目标上保留 half-float 精度——f1 附件会在 IDT 前把高位深
            # 编码值量化到 8-bit（与 FX-M01 的 f2 scratch 策略对齐）。
            output_tex = ctx.texture(
                (width, height), 4,
                dtype=self._source_rgb_output_dtype(bit_depth))
            output_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            output_tex.repeat_x = False
            output_tex.repeat_y = False
            output_fbo = ctx.framebuffer(color_attachments=[output_tex])
            output_fbo.use()
            ctx.viewport = (0, 0, width, height)
            ctx.clear(0.0, 0.0, 0.0, 1.0)

            mode = self._classify_planar_upload_mode(pixel_format, len(planes), bit_depth)
            uploader = self._yuv_uploader

            # Configure the YUV->RGB matrix and signal range from source
            # metadata BEFORE the render call. The uploader's shader reads
            # these to expand YUV->RGB; setting them after upload_nv12/upload_*
            # but before render_*_to_rgb is sufficient (they only affect the
            # render step). rgb_planar mode carries no YUV matrix, so skip it.
            if mode != "rgb_planar":
                from src.shared.video.yuv_gpu_upload import YUVGPUUploader
                if matrix_tag == "bt2020":
                    uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT2020)
                elif matrix_tag == "bt601":
                    uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT601)
                else:
                    uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
                if range_tag == "full":
                    uploader.set_color_range(YUVGPUUploader.RANGE_FULL)
                else:
                    uploader.set_color_range(YUVGPUUploader.RANGE_LIMITED)

            if mode == "nv12":
                y_tex, uv_tex = uploader.upload_nv12(upload_planes[0], upload_planes[1], frame_idx, bit_depth)
                uploader.render_nv12_to_rgb(y_tex, uv_tex, output_fbo, bit_depth=bit_depth)
            elif mode in ("p010", "p016"):
                y_tex, uv_tex = uploader.upload_p010(upload_planes[0], upload_planes[1], frame_idx)
                uploader.render_nv12_to_rgb(
                    y_tex,
                    uv_tex,
                    output_fbo,
                    bit_depth=10 if mode == "p010" else 16,
                )
            elif mode == "yuv444":
                texes = uploader.upload_yuv444(
                    upload_planes[0], upload_planes[1], upload_planes[2], frame_idx, bit_depth,
                    a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                uploader.render_yuv_to_rgb(
                    texes[0], texes[1], texes[2], output_fbo, bit_depth=bit_depth,
                    a_tex=texes[3] if len(texes) >= 4 else None,
                    alpha_bit_depth=alpha_bit_depth,
                    alpha_premultiplied=alpha_premult)
            elif mode == "yuv422":
                texes = uploader.upload_yuv422(
                    upload_planes[0], upload_planes[1], upload_planes[2], frame_idx, bit_depth,
                    a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                uploader.render_yuv_to_rgb(
                    texes[0], texes[1], texes[2], output_fbo, bit_depth=bit_depth,
                    a_tex=texes[3] if len(texes) >= 4 else None,
                    alpha_bit_depth=alpha_bit_depth,
                    alpha_premultiplied=alpha_premult)
            elif mode == "rgb_planar" and len(planes) >= 3:
                # gbrp*/gbrap* — upload in native G,B,R order and bypass the
                # YUV matrix in the shader (gbr_mode)；按 YUV 矩阵渲染 GBR
                # 平面会得到错误颜色（T-03 修正）。gbrap* 第四平面是 Alpha。
                texes = uploader.upload_yuv444(
                    upload_planes[0], upload_planes[1], upload_planes[2], frame_idx, bit_depth,
                    a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                uploader.render_yuv_to_rgb(
                    texes[0], texes[1], texes[2], output_fbo, bit_depth=bit_depth,
                    a_tex=texes[3] if len(texes) >= 4 else None,
                    alpha_bit_depth=alpha_bit_depth,
                    alpha_premultiplied=alpha_premult,
                    gbr_mode=True)
            else:
                if len(planes) < 3:
                    output_fbo.release()
                    output_tex.release()
                    return None
                texes = uploader.upload_yuv420(
                    upload_planes[0], upload_planes[1], upload_planes[2], frame_idx, bit_depth,
                    a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                uploader.render_yuv_to_rgb(
                    texes[0], texes[1], texes[2], output_fbo, bit_depth=bit_depth,
                    a_tex=texes[3] if len(texes) >= 4 else None,
                    alpha_bit_depth=alpha_bit_depth,
                    alpha_premultiplied=alpha_premult)

            output_fbo.release()
            # Same lifetime rule as _upload_decoded_frame: the previous texture
            # may still be cached; only cache eviction / clear_* may release it.
            self._owned_textures[output_tex.glo] = output_tex
            self._last_source_texture_id = output_tex.glo

            return _TexResult(
                texture_id=output_tex.glo,
                width=width,
                height=height,
                # The GPU YUV→RGB shader applies matrix+range only; the RGB
                # result is still in the source's encoded transfer, so tag it
                # with the detected transfer ("bt709"/"pq"/"hlg"), not "srgb".
                color_space=cs_tag,
                alpha_premultiplied=False,
                bit_depth=bit_depth,
            )

        except Exception as e:
            logger.debug(f"Planar frame GPU upload failed: {e}")
            return None

    @staticmethod
    def _classify_planar_upload_mode(pixel_format: str, num_planes: int, bit_depth: int) -> str:
        """Classify planar frame format for YUVGPUUploader routing."""
        pf = (pixel_format or "").lower().strip()
        if num_planes == 2:
            if pf == "p016":
                return "p016"
            if pf == "p010" or bit_depth > 8:
                return "p010"
            return "nv12"
        if "gbrp" in pf or "gbrap" in pf:
            # 覆盖 topos_gbrap*（alpha 变体，不含 "gbrp" 子串）；此前落入
            # yuv420 兜底导致几何断言失败（T-03 修正）
            return "rgb_planar"
        if "444" in pf:
            return "yuv444"
        if "422" in pf:
            return "yuv422"
        if "nv12" in pf or "nv21" in pf:
            return "nv12"
        if "p010" in pf:
            return "p010"
        if "p016" in pf:
            return "p016"
        return "yuv420"

    @staticmethod
    def _prepare_planar_upload_planes(planes: Any) -> tuple[np.ndarray, ...]:
        """Convert top-left CPU planes to bottom-left GL upload buffers."""
        return tuple(
            np.ascontiguousarray(np.flipud(np.asarray(plane)))
            for plane in planes
        )

    # ------------------------------------------------------------------
    # Fallback 3: Debug placeholder texture
    # ------------------------------------------------------------------

    def _create_debug_placeholder(self, width: int, height: int) -> _TexResult | None:
        """
        创建调试占位符 texture。

        特征：
        - 深灰背景 (64, 64, 64)
        - 红色水平条 (200, 40, 40)，占据中间 1/3
        - 让用户能区分 "缺少纹理" 和 "黑色视频"

        注意：此方法缓存 texture，同一尺寸不会重复创建。
        但它必须运行在 GL 上下文有效的线程上。
        """
        cache_key = (width, height)
        if cache_key in self._debug_placeholder_cache:
            return self._debug_placeholder_cache[cache_key]

        try:
            import moderngl
            ctx = self._gl_context
            if ctx is None:
                # No GL context - cannot create placeholder
                logger.warning("TimelineClipFrameProvider: no GL context for debug placeholder")
                return None

            # Build placeholder image: deep gray + red bar
            img = self._build_debug_image(width, height)

            # Upload as RGB texture
            tex = ctx.texture((width, height), 3, data=img, dtype='f1')
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = False
            tex.repeat_y = False
            self._owned_textures[tex.glo] = tex

            result = _TexResult(
                texture_id=tex.glo,
                width=width,
                height=height,
                revision=0,
                color_space="srgb",
                alpha_premultiplied=False,
            )

            self._debug_placeholder_cache[cache_key] = result
            logger.info(
                f"TimelineClipFrameProvider: created debug placeholder "
                f"{width}x{height}, tex_id={tex.glo}"
            )
            return result

        except Exception as e:
            logger.warning(f"TimelineClipFrameProvider: debug placeholder failed: {e}")
            return None

    @staticmethod
    def _build_debug_image(width: int, height: int) -> np.ndarray:
        """
        构建调试占位符图像数据。

        深灰背景 + 红色水平条。
        返回形状为 (height, width, 3) 的 uint8 RGB numpy 数组。
        """
        # Deep gray background
        img = np.full((height, width, 3), 64, dtype=np.uint8)

        # Red horizontal bar in the middle third
        bar_y_start = height // 3
        bar_y_end = 2 * height // 3
        img[bar_y_start:bar_y_end, :, :] = [200, 40, 40]

        # Thin white border lines to make it more visible
        border_thickness = max(1, height // 100)
        if border_thickness >= 1:
            # Top edge of bar
            img[bar_y_start:bar_y_start + border_thickness, :, :] = [220, 220, 220]
            # Bottom edge of bar
            img[bar_y_end - border_thickness:bar_y_end, :, :] = [220, 220, 220]

        return np.ascontiguousarray(img)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def clear_debug_cache(self) -> None:
        """释放全部 owned 纹理/源缓存/调试占位符/解码会话。

        AUD-18325：该方法名不副实——虽然叫 "debug cache"，实际会释放
        本 provider 持有的**所有** GPU 资源。因此它与
        :meth:`clear_source_cache` 遵守同一跨线程契约：ModernGL 纹理
        只能在做 GL 工作的渲染线程上释放；从其他线程调用时把请求停靠
        在标志位上，由渲染线程下一次 ``get_timeline_clip_texture`` 入口
        统一 drain，这里不做任何 GL 调用。
        """
        if (
            self._gl_thread_id is not None
            and threading.get_ident() != self._gl_thread_id
        ):
            # 非 GL 线程：停靠完整释放请求，绝不在此线程触碰 GL 对象
            self._source_cache_clear_pending = True
            self._debug_cache_clear_pending = True
            return
        self._clear_debug_cache_unlocked()

    def _clear_debug_cache_unlocked(self) -> None:
        """Perform the full debug/owned-resource release; GL-thread only."""
        self._release_external_gpu_resources(wait=True)
        for tex in self._owned_textures.values():
            try:
                tex.release()
            except Exception as e:
                logger.debug(
                    f"TimelineClipFrameProvider: owned texture release failed: {e}"
                )
        self._owned_textures.clear()
        self._last_source_texture_id = None
        self._debug_placeholder_cache.clear()
        self._source_texture_cache.clear()
        self._source_cache_order.clear()
        self.release_video_sessions()

    def clear_source_cache(self) -> None:
        """Clear source frame texture cache. Called when clip changes.

        Thread contract: releasing owned ModernGL textures is only safe on
        the GL/render thread that owns the context, and the
        ``_source_texture_cache``/``_source_cache_order`` pair must only be
        mutated from one thread at a time (a UI-thread clear interleaved
        with the render thread's LRU touch can leave orphaned or duplicated
        order entries and silently defeat ``_SOURCE_CACHE_MAX``).  When
        called from a thread other than the one that performed this
        provider's GL work, park the request on a flag — the render thread
        drains it at the next ``get_timeline_clip_texture`` entry — and only
        the GL-free bookkeeping is cleared immediately.
        """
        if (
            self._gl_thread_id is not None
            and threading.get_ident() != self._gl_thread_id
        ):
            self._source_cache_clear_pending = True
            self._proxy_unavailable_assets.clear()
            return
        self._source_cache_clear_pending = False
        self._clear_source_cache_unlocked()

    def _clear_source_cache_unlocked(self) -> None:
        """Perform the actual cache clear; GL-thread only."""
        for key, result in list(self._source_texture_cache.items()):
            if result.texture_id in self._owned_textures:
                # AUD-18321: 换 clip 清缓存同样经 retired 队列延迟释放，
                # 与淘汰路径一致（最后一次求值可能仍在采样旧纹理）。
                self._retire_source_texture(result.texture_id)
        self._source_texture_cache.clear()
        self._source_cache_order.clear()
        self._proxy_unavailable_assets.clear()

    def _drain_pending_source_cache_clear(self) -> None:
        """Render-thread entry hook: execute a parked cross-thread clear."""
        if self._source_cache_clear_pending:
            self._source_cache_clear_pending = False
            self._clear_source_cache_unlocked()
        if getattr(self, "_debug_cache_clear_pending", False):
            # AUD-18325：停靠的 clear_debug_cache 完整释放（GL 线程上执行）
            self._debug_cache_clear_pending = False
            self._clear_debug_cache_unlocked()

    def _invalidate_gl_thread_state(self, new_thread_id: int) -> None:
        """AUD-18323: defensive rebuild when the GL thread identity changes.

        旧上下文的 owned 纹理无法在新线程上安全释放（跨上下文
        glDelete*），这里只作废引用与记账——GPU 内存在旧 context 销毁时
        由驱动回收——随后把跨线程 clear 协议重新绑定到新线程。
        """
        self._source_texture_cache.clear()
        self._source_cache_order.clear()
        self._source_texture_retired.clear()
        self._debug_placeholder_cache.clear()
        self._owned_textures.clear()
        self._last_source_texture_id = None
        self._source_cache_clear_pending = False
        self._debug_cache_clear_pending = False
        self._gl_thread_id = new_thread_id

    def release_video_sessions(self) -> None:
        """Release this provider's references to shared decode sessions."""
        self._release_external_gpu_resources(wait=True)
        # 换上下文：作废后台跳转解码与待上传帧（渲染线程上下文，不 join）
        self._stop_async_decode_thread(join=False)
        with self._async_lock:
            self._async_targets.clear()
            self._pending_source_frames.clear()
        self._asset_last_frame.clear()
        tags_cache = getattr(self, '_clip_color_tags', None)
        if tags_cache is not None:
            tags_cache.clear()
        service = self._frame_provider_service
        if service is not None:
            close_clip = getattr(service, "close_clip", None)
            if callable(close_clip):
                for clip_id in self._video_session_ids.values():
                    try:
                        close_clip(clip_id)
                    except Exception:
                        pass
        self._video_session_ids.clear()
        self._frame_provider_service = None
        # Proxy availability may change with a new session/project.
        self._proxy_unavailable_assets.clear()

    def _release_owned_texture(self, tex_id: int) -> None:
        tex = self._owned_textures.pop(tex_id, None)
        if tex is None:
            return
        try:
            tex.release()
        except Exception:
            pass
