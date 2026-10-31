"""视频降噪编排管线：时域 → 空域（计划推荐顺序）。

对应计划 M5/M6/M13：
- 接入点在视频纹理准备完成后、调色节点执行前（_do_render_impl 的
  source_tex 选择之后），CPU 上传与 GPU 外部纹理两条路径在此汇合。
- 功能开关（ConfigManager）：video_denoise_enabled /
  video_temporal_denoise_enabled / video_spatial_denoise_enabled /
  video_denoise_backend / video_denoise_debug_overlay
- 回退链：时域+空域 → 空域 → 原始帧；任何异常不阻塞播放。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Hashable, Optional, Sequence

import moderngl

from src.shared.config.config_manager import ConfigManager
from src.shared.video.denoise.params import VideoDenoiseParams

from .video_denoise_gl import QuadRenderer, RenderSurfaceCache
from .video_spatial_denoise import VideoSpatialDenoiser
from .video_temporal_denoise import VideoTemporalDenoiser

logger = logging.getLogger(__name__)


def _cfg_bool(key: str, default: bool) -> bool:
    try:
        value = ConfigManager().get(key)
        return bool(value) if value is not None else default
    except Exception:
        return default


class VideoDenoisePipeline:
    """时域 + 空域视频降噪编排器（渲染线程专用）。"""

    def __init__(self, ctx: moderngl.Context) -> None:
        self._ctx = ctx
        self._quad = QuadRenderer(ctx)
        self._surfaces = RenderSurfaceCache(ctx)
        self._spatial = VideoSpatialDenoiser(ctx, self._quad, self._surfaces)
        self._temporal = VideoTemporalDenoiser(ctx, self._quad, self._surfaces)
        self._params = VideoDenoiseParams()
        self._quality_mode = "preview"
        self._enabled_flag = _cfg_bool("video_denoise_enabled", True)
        self._temporal_flag = _cfg_bool("video_temporal_denoise_enabled", True)
        self._spatial_flag = _cfg_bool("video_spatial_denoise_enabled", True)
        self._debug_overlay_flag = _cfg_bool("video_denoise_debug_overlay", False)
        self._last_error_time = 0.0
        self._error_count = 0
        # 性能统计（渲染线程内更新，快照读取）
        # AUD-0903：stats 供其他线程 `dict(self.stats)` 快照读取，运行期
        # 向 dict 插入新键（扩容/resize）会让并发快照抛
        # "dictionary changed size during iteration"——__init__ 预置全部
        # 键（含 window_frames），此后只做原地更新，杜绝运行期扩容。
        self.stats: Dict[str, Any] = {
            "frames": 0,
            "window_frames": 0,
            "temporal_ms": 0.0,
            "spatial_ms": 0.0,
            "total_ms": 0.0,
            "last_temporal_ms": 0.0,
            "last_spatial_ms": 0.0,
            "last_total_ms": 0.0,
        }

    # ------------------------------------------------------------------
    # 参数更新（UI 线程经渲染命令调用，命令在 GL mutex 下执行）

    def set_params(self, params: VideoDenoiseParams, quality_mode: str = "preview") -> None:
        if not isinstance(params, VideoDenoiseParams):
            return
        self._params = params.sanitized()
        if quality_mode in ("preview", "paused", "export"):
            self._quality_mode = quality_mode

    @property
    def params(self) -> VideoDenoiseParams:
        """当前降噪参数（只读快照）。

        AUD-0905: 返回 sanitized 后的副本而非内部对象——调用方在 GL
        mutex 之外拿到的是与渲染线程一致的只读快照；即使未来
        ``VideoDenoiseParams`` 改为可变 dataclass，也无法经此引用绕过
        ``set_params`` 的 sanitized/锁直接改参。
        """
        return self._params.sanitized()

    def reset_temporal_history(self) -> None:
        self._temporal.force_reset()

    def set_debug_overlay(self, enabled: bool) -> None:
        """调试叠加（运动置信度可视化）开关。"""
        self._debug_overlay_flag = bool(enabled)

    def reload_flags(self) -> None:
        self._enabled_flag = _cfg_bool("video_denoise_enabled", True)
        self._temporal_flag = _cfg_bool("video_temporal_denoise_enabled", True)
        self._spatial_flag = _cfg_bool("video_spatial_denoise_enabled", True)
        self._debug_overlay_flag = _cfg_bool("video_denoise_debug_overlay", False)

    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        return (
            self._enabled_flag
            and self._params.is_active()
            and (self._spatial_flag or self._temporal_flag)
        )

    def process(
        self,
        source_tex: moderngl.Texture,
        frame_index: int,
        generation_id: int,
        clip_key: Hashable,
        quality_mode: Optional[str] = None,
    ) -> moderngl.Texture:
        """对视频源纹理执行降噪；返回输出纹理（失败时返回原始纹理）。

        quality_mode 可由调用方按渲染上下文覆盖（playback→preview，
        paused 静态预览→paused，导出→export）；缺省用 set_params 的值。
        """
        if not self.is_active():
            return source_tex

        mode = quality_mode if quality_mode in ("preview", "paused", "export") \
            else self._quality_mode
        effective = self._params.for_quality(mode)
        params_key = effective.parameters_key()

        t0 = time.perf_counter()
        tex = source_tex
        try:
            if self._temporal_flag and effective.temporal.is_active():
                tex = self._temporal.process(
                    tex,
                    frame_index,
                    generation_id,
                    clip_key,
                    effective.temporal,
                    params_key,
                )
                if self._debug_overlay_flag and tex is not source_tex:
                    tex = self._temporal.debug_overlay(tex)
            t1 = time.perf_counter()
            if self._spatial_flag and effective.spatial.is_active():
                tex = self._spatial.process(tex, effective.spatial)
            t2 = time.perf_counter()
        except Exception:
            logger.exception("VideoDenoisePipeline: process failed, passthrough")
            self._note_error()
            return source_tex

        # 更新统计
        self.stats["frames"] += 1
        self.stats["last_temporal_ms"] = (t1 - t0) * 1000.0
        self.stats["last_spatial_ms"] = (t2 - t1) * 1000.0
        self.stats["last_total_ms"] = (t2 - t0) * 1000.0
        # 滑动平均
        n = self.stats["frames"]
        self.stats["temporal_ms"] += (
            self.stats["last_temporal_ms"] - self.stats["temporal_ms"]
        ) / max(1, min(n, 120))
        self.stats["spatial_ms"] += (
            self.stats["last_spatial_ms"] - self.stats["spatial_ms"]
        ) / max(1, min(n, 120))
        self.stats["total_ms"] += (
            self.stats["last_total_ms"] - self.stats["total_ms"]
        ) / max(1, min(n, 120))
        return tex

    # ------------------------------------------------------------------
    # 窗口模式（Paused/Export 由调用方提供邻帧纹理）

    def process_window(
        self,
        source_tex: moderngl.Texture,
        neighbor_textures: Sequence[moderngl.Texture],
        params: Optional[VideoDenoiseParams] = None,
        quality_mode: Optional[str] = None,
    ) -> moderngl.Texture:
        mode = quality_mode if quality_mode in ("preview", "paused", "export") \
            else self._quality_mode
        p = (params or self._params).for_quality(mode)
        if not self._enabled_flag or not p.temporal.is_active() or not self._temporal_flag:
            return source_tex
        try:
            tex = self._temporal.process_window(
                neighbor_textures, source_tex, p.temporal
            )
            # AUD-0903：window_frames 已在 __init__ 预置，此处原地自增
            # （旧写法 .get+首次插入会在快照读取并发时 resize dict）
            self.stats["window_frames"] += 1
            if self._debug_overlay_flag and tex is not source_tex:
                tex = self._temporal.debug_overlay(tex)
        except Exception:
            logger.exception("VideoDenoisePipeline: window process failed")
            return source_tex
        if self._spatial_flag and p.spatial.is_active():
            tex = self._spatial.process(tex, p.spatial)
        return tex

    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        stats = dict(self.stats)
        stats["temporal"] = dict(self._temporal.stats)
        stats["surfaces"] = self._surfaces.surface_count
        stats["surface_bytes"] = self._surfaces.total_bytes()
        stats["enabled"] = self.is_active()
        return stats

    def _note_error(self) -> None:
        self._error_count += 1
        now = time.perf_counter()
        if now - self._last_error_time > 60.0:
            self._last_error_time = now
            self._error_count = 1
        elif self._error_count > 30:
            # 持续失败：自动关闭，避免热路径反复异常
            logger.error("VideoDenoisePipeline: too many errors, disabling")
            self._enabled_flag = False

    def release(self) -> None:
        self._spatial.release()
        self._temporal.release()
        self._surfaces.release_all()
        self._quad.release()
