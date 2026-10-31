"""视频导出降噪桥：headless GL context + VideoDenoisePipeline 复用。

P1-COLOR-13 收口：把 Color 页的 GPU 视频降噪接入视频导出（Deliver
队列 / 单片导出共用 TimelineExporter 渲染管线）。导出 worker 线程
无 ModernGL 上下文，此桥惰性创建 standalone context，复用与预览
完全相同的 VideoDenoisePipeline（时域因果累积 + 空域增强），保证
导出与预览像素一致；context 创建失败或处理异常时直通原帧，导出
永不因降噪中断。

时域语义：导出循环按时间线帧序推进，同一 clip 的 source_frame
逐帧 +1，满足因果累积的 delta=+1；clip 切换 / 变速跳帧由 pipeline
状态机按 clip_key / frame_jump 自动重置（转场 wipe 预合成分支与
主循环互斥取帧，无同帧重复处理）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Hashable, Optional

import numpy as np

logger = logging.getLogger(__name__)


class ExportVideoDenoiseBridge:
    """导出线程专用的 GPU 视频降噪桥（BGR 帧 → GPU 降噪 → 同格式帧）。

    生命周期由 TimelineExporter 管理：首帧需要降噪时惰性初始化，
    导出结束时 ``release()`` 释放 context。非线程安全——每个导出
    worker 线程持有独立实例（standalone context 不能跨线程共享）。
    """

    def __init__(self) -> None:
        """初始化空桥：所有 GL 资源延迟到首帧 ``process_frame`` 时创建。"""
        self._ctx = None
        self._pipeline = None
        self._input_tex = None
        # AUD-0418: 按输出纹理身份缓存的读回 FBO（长序列导出避免逐帧
        # 创建/销毁 FBO 对象的分配 churn）。
        self._out_fbo: Optional[Any] = None
        self._out_fbo_tex: Optional[Any] = None
        self._failed = False
        self._warned_process_error = False

    @property
    def available(self) -> bool:
        """GL context 与降噪管线是否已就绪。"""
        return self._pipeline is not None

    def _ensure_ready(self) -> bool:
        """惰性创建 standalone context + 降噪管线；失败后不再重试。"""
        if self._pipeline is not None:
            return True
        if self._failed:
            return False
        try:
            import moderngl

            # 先建 context 再 import 管线：context 失败时完全不触碰
            # video_denoise 模块（也避免测试替身污染模块级 moderngl 绑定）
            self._ctx = moderngl.create_standalone_context(require=330)

            from .video_denoise_pipeline import VideoDenoisePipeline

            self._pipeline = VideoDenoisePipeline(self._ctx)
            logger.info(
                "ExportVideoDenoiseBridge: headless GL context ready "
                f"(renderer={self._ctx.info.get('GL_RENDERER', '?')})"
            )
            return True
        except Exception as error:
            self._failed = True
            logger.warning(
                f"ExportVideoDenoiseBridge: headless GL unavailable "
                f"({error}); export denoise passthrough"
            )
            return False

    def process_frame(
        self,
        frame_bgr: np.ndarray,
        clip_key: Hashable,
        source_frame: int,
        params_dict: Dict[str, Any],
    ) -> np.ndarray:
        """对一帧 BGR 8/16-bit 图像执行导出档 GPU 降噪。

        失败/未启用/参数未激活时原样返回输入帧。

        Args:
            frame_bgr: HxWx3(+) BGR 帧，uint8/uint16 整型或 float 源；
                原帧不被修改，降噪结果写入返回副本的 RGB 通道。
            clip_key: 时域累积的 clip 标识（与预览管线同键空间）。
            source_frame: 该帧在源 clip 中的帧号，驱动因果累积与
                跳帧重置。
            params_dict: ``VideoDenoiseParams.from_dict`` 兼容的降噪
                参数字典。

        Returns:
            与输入同 dtype/shape 的 BGR 帧；任何直通分支返回原对象。
        """
        if not isinstance(params_dict, dict) or not params_dict:
            return frame_bgr
        try:
            from src.shared.video.denoise.params import VideoDenoiseParams

            params = VideoDenoiseParams.from_dict(params_dict)
        except Exception:
            return frame_bgr
        if not params.is_active():
            return frame_bgr
        if not self._ensure_ready():
            return frame_bgr
        try:
            if not isinstance(frame_bgr, np.ndarray):
                return frame_bgr
            if frame_bgr.ndim != 3 or frame_bgr.shape[2] < 3:
                return frame_bgr
            # 全局偏好开关（video_denoise_enabled 等）由 pipeline 尊重，
            # 与预览行为一致
            self._pipeline.set_params(params, "export")
            if not self._pipeline.is_active():
                return frame_bgr

            height, width = frame_bgr.shape[:2]
            max_value = (
                float(np.iinfo(frame_bgr.dtype).max)
                if np.issubdtype(frame_bgr.dtype, np.integer)
                else 1.0
            )
            # BGR → RGBA float [0,1] 上传（f2 精度覆盖 10/12-bit 源）
            rgb = frame_bgr[..., :3][..., ::-1].astype(np.float32) / max_value
            rgba = np.dstack([rgb, np.ones_like(rgb[..., :1])])
            tex = self._input_tex
            if tex is None or tex.size != (width, height):
                if tex is not None:
                    tex.release()
                tex = self._ctx.texture((width, height), 4, dtype="f2")
                self._input_tex = tex
            tex.write(np.ascontiguousarray(rgba.astype(np.float16)))

            out_tex = self._pipeline.process(
                tex,
                int(source_frame),
                0,
                clip_key,
                quality_mode="export",
            )
            if out_tex is None:
                return frame_bgr

            # AUD-0418: 输出纹理稳定时复用缓存 FBO，避免逐帧创建/销毁。
            fbo = self._get_or_create_out_fbo(out_tex)
            try:
                data = fbo.read(components=3, dtype="f4")
            except Exception:
                # 缓存 FBO 可能因管线重建输出表面而失效——丢弃缓存重建一次
                logger.debug("out FBO read failed; rebuilding cache FBO once",
                             exc_info=True)
                self._drop_out_fbo()
                fbo = self._get_or_create_out_fbo(out_tex)
                data = fbo.read(components=3, dtype="f4")
            arr = (
                np.frombuffer(data, dtype=np.float32)
                .reshape(height, width, 3)[..., ::-1]
            )
            result = frame_bgr.copy()
            result[..., :3] = np.rint(
                np.clip(arr, 0.0, 1.0) * max_value
            ).astype(frame_bgr.dtype)
            return result
        except Exception:
            if not self._warned_process_error:
                self._warned_process_error = True
                logger.exception(
                    "ExportVideoDenoiseBridge: process failed; passthrough"
                )
            return frame_bgr

    def _get_or_create_out_fbo(self, out_tex: Any) -> Any:
        """返回包裹 ``out_tex`` 的缓存读回 FBO（AUD-0418）。

        按纹理对象身份缓存：同一输出纹理复用同一 FBO，纹理更换时释放
        旧 FBO 并重建。管线内部表面被销毁重建导致的失效由调用方的
        重建重试兜底。
        """
        if self._out_fbo is not None and self._out_fbo_tex is out_tex:
            return self._out_fbo
        self._drop_out_fbo()
        fbo = self._ctx.framebuffer(out_tex)
        self._out_fbo = fbo
        self._out_fbo_tex = out_tex
        return fbo

    def _drop_out_fbo(self) -> None:
        """释放缓存的输出 FBO（幂等）。"""
        if self._out_fbo is not None:
            try:
                self._out_fbo.release()
            except Exception:
                logger.debug("Failed to release cached output FBO", exc_info=True)
            self._out_fbo = None
        self._out_fbo_tex = None

    def release(self) -> None:
        """释放降噪管线、输入纹理与 GL context（幂等）。"""
        # AUD-0418: 先释放包着管线表面/输入纹理的 FBO，再释放其所属资源，
        # 避免对已销毁纹理做隐式解绑。
        self._drop_out_fbo()
        if self._pipeline is not None:
            try:
                self._pipeline.release()
            except Exception:
                pass
            self._pipeline = None
        if self._input_tex is not None:
            try:
                self._input_tex.release()
            except Exception:
                pass
            self._input_tex = None
        if self._ctx is not None:
            try:
                self._ctx.release()
            except Exception:
                pass
            self._ctx = None
