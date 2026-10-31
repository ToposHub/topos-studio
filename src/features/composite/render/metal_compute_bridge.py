"""GL texture ↔ Metal compute 的 CPU staging 桥（计划 P4-02/P4-04）。

macOS 的 OpenGL 4.1 与 Metal 之间不存在可用的纹理共享通道（IOSurface
桥需要 ``CGLTexImageIOSurface2D``，ModernGL 不暴露该入口），因此 Metal
compute 的输入输出经 CPU staging buffer 中转：

    GL texture → ``tex.read`` (RGBA16F) → MTLBuffer → Metal kernel
    → read back → ``tex.write`` → GL texture (RGBA16F)

这替换的是原来「GL compute 不可用 → fragment fallback（自认为更慢）」
的路径：读回 + 上传的固定成本（4K 约 2×67 MB，512² 直方图约 4 MB）
由内核成本摊销，实际收益由 P4-05 的 A/B 报告按 K5 判定。

归属 feature 侧（``module_boundary_rules``）：桥知道 ModernGL 纹理与
``resource_pool`` 契约；Metal 设备细节在 ``src/shared/gpu/metal/``。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

import numpy as np

from src.shared.gpu.metal import MetalComputeContext, MetalComputeError, MetalKernel
from src.shared.gpu.metal.kernels import (
    METAL_COMPUTE_SOURCES,
    make_blur_params,
    make_erode_dilate_params,
)

logger = logging.getLogger(__name__)


class MetalComputeBridge:
    """用 Metal compute 处理 ModernGL 纹理的大半径效果（模糊/形态学）。

    线程契约：与 ``ComputeShaderManager`` 相同（render / export 线程并发
    调用安全——每次 ``apply_*`` 自成一体，Metal 端由 context 内锁保护）。
    """

    def __init__(self, gl_ctx: Any) -> None:
        """
        Args:
            gl_ctx: 调用方（ComputeShaderManager）的 ModernGL context，
                用于在正确的 context 上创建输出纹理（resource_pool 缺席
                时的兜底路径）。
        """
        self._gl_ctx = gl_ctx
        self._ctx = MetalComputeContext()
        self._lock = threading.Lock()
        self._kernels: Dict[str, MetalKernel] = {}

    @classmethod
    def is_supported(cls) -> bool:
        return MetalComputeContext.is_supported()

    def _kernel(self, name: str) -> MetalKernel:
        with self._lock:
            kernel = self._kernels.get(name)
        if kernel is not None:
            return kernel
        kernel = self._ctx.compile_kernel(name, METAL_COMPUTE_SOURCES[name])
        with self._lock:
            self._kernels[name] = kernel
        return kernel

    # ------------------------------------------------------------------
    # 效果入口（与 ComputeShaderManager 的两个入口语义对齐）
    # ------------------------------------------------------------------

    def apply_blur(
        self,
        input_tex: Any,
        radius: float,
        w: int,
        h: int,
        resource_pool: Optional[object] = None,
        edge_policy: int = 0,
    ) -> Optional[Any]:
        """可分离高斯模糊（水平 + 垂直两趟，对应 apply_blur_compute）。"""
        try:
            arr = self._read_texture(input_tex)
            # 与 GLSL 版的变体选择一致：radius > 32 用 tile 优化版
            use_shared = radius > 32.0
            name_h = "blur_h_shared" if use_shared else "blur_h"
            name_v = "blur_v_shared" if use_shared else "blur_v"
            params = make_blur_params(w, h, radius, edge_policy)
            param_buf = self._ctx.create_buffer_from_numpy(params)

            src = self._ctx.create_buffer_from_numpy(arr)
            tmp = self._ctx.create_buffer(arr.nbytes)
            dst = self._ctx.create_buffer(arr.nbytes)
            self._dispatch_blur_pass(name_h, src, tmp, param_buf, w, h)
            self._dispatch_blur_pass(name_v, tmp, dst, param_buf, w, h)
            self._ctx.finish()  # 单次同步点：两趟 kernel 一个 commit

            out_arr = np.frombuffer(
                self._ctx.read_buffer(dst), dtype=np.float16
            ).reshape(h, w, 4)
            return self._write_texture(out_arr, resource_pool)
        except MetalComputeError as exc:
            logger.error("Metal 模糊执行失败: %s", exc)
            raise
        except Exception as exc:  # noqa: BLE001 — 统一回退语义由调用方处理
            logger.error("Metal 模糊桥异常: %s", exc)
            raise MetalComputeError(f"Metal 模糊桥异常: {exc}") from exc

    def apply_erode_dilate(
        self,
        input_tex: Any,
        operation: str,
        size: int,
        w: int,
        h: int,
        resource_pool: Optional[object] = None,
        channels: int = 0,
    ) -> Optional[Any]:
        """形态学腐蚀/膨胀（对应 apply_erode_dilate_compute）。"""
        try:
            arr = self._read_texture(input_tex)
            name = "erode_dilate_shared" if size > 16 else "erode_dilate"
            params = make_erode_dilate_params(w, h, operation, size, channels)
            param_buf = self._ctx.create_buffer_from_numpy(params)
            src = self._ctx.create_buffer_from_numpy(arr)
            dst = self._ctx.create_buffer(arr.nbytes)
            kernel = self._kernel(name)
            self._ctx.dispatch(
                kernel, buffers={0: src, 1: dst, 2: param_buf},
                threads=(w, h, 1),
            )
            self._ctx.finish()

            out_arr = np.frombuffer(
                self._ctx.read_buffer(dst), dtype=np.float16
            ).reshape(h, w, 4)
            return self._write_texture(out_arr, resource_pool)
        except MetalComputeError as exc:
            logger.error("Metal 形态学执行失败: %s", exc)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Metal 形态学桥异常: %s", exc)
            raise MetalComputeError(f"Metal 形态学桥异常: {exc}") from exc

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _dispatch_blur_pass(self, name: str, src: Any, dst: Any,
                            param_buf: Any, w: int, h: int) -> None:
        kernel = self._kernel(name)
        if name == "blur_h_shared":
            threads = (((w + 63) // 64) * 64, h, 1)
            group = (64, 1, 1)
        elif name == "blur_v_shared":
            threads = (w, ((h + 63) // 64) * 64, 1)
            group = (1, 64, 1)
        else:
            threads = (w, h, 1)
            group = None
        self._ctx.dispatch(
            kernel, buffers={0: src, 1: dst, 2: param_buf},
            threads=threads, group=group,
        )

    def _read_texture(self, tex: Any) -> np.ndarray:
        """读回 GL 纹理为 (h, w, 4) float16 数组（保持行序，桥为直通）。

        ``Texture.read()`` 按纹理内部格式返回字节（RGBA16F → 4×f2）。
        """
        raw = tex.read()
        h, w = tex.height, tex.width
        expected = h * w * 4 * 2
        if len(raw) != expected:
            raise MetalComputeError(
                f"GL 纹理读回尺寸不符：{len(raw)} != {expected}"
                "（桥仅支持 RGBA16F 输入）"
            )
        return np.frombuffer(raw, dtype="<f2").reshape(h, w, 4)

    def _write_texture(self, arr: np.ndarray, resource_pool: Optional[object]) -> Any:
        """把 RGBA16F 结果写回新的 GL 纹理（优先走 resource_pool）。"""
        if resource_pool is not None:
            tex = resource_pool.acquire_texture(
                arr.shape[1], arr.shape[0], components=4, dtype="f2"
            )
        else:
            # 兜底：在调用方传入的 GL context 上直接创建（与
            # ComputeShaderManager._create_output_texture 的直建路径一致）
            tex = self._gl_ctx.texture(
                (arr.shape[1], arr.shape[0]), 4, dtype="f2"
            )
            tex.filter = (self._gl_ctx.LINEAR, self._gl_ctx.LINEAR)
        tex.write(np.ascontiguousarray(arr, dtype="<f2").tobytes())
        return tex
