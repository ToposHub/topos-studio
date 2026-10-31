"""M17.1b · GPU 按监看目标缩放后回读候选路径（离屏可验证软件子项）。

M17 实施："先 GPU 按监看目标缩放再回读，减少 33MB 整帧回读和 CPU
resize"。与 M17.1a 的 UYVY 打包同为候选路径——真实监看窗口开/关
A/B 裁决与 DeckLink 实机对拍仍按 deferred_gates 延期（需真机）。

本模块提供 GPU 缩放+回读的实现与离屏度量：

- ``gpu_scale_readback``：源帧（RGBA uint8/float）在 GPU 纹理上经
  采样器双线性缩放到监看目标尺寸后回读——回读字节 = 目标尺寸而非
  整帧（4K→1080p 监看 ≈ 33MB→8.2MB）；
- ``cpu_resize_baseline``：cv2.resize 基线（现状路径）；
- ``ab_metrics``：A/B 度量（回读字节比/峰值差）——离屏 A/B 数据与
  真实窗口复测同口径。

GPU 不可用（无 moderngl 上下文/离屏限制）返回 None——调用方回退
CPU 路径，不中断监看。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

_SCALE_FS = """
#version 330
uniform sampler2D u_tex;
in vec2 v_uv;
out vec4 f_color;
void main() {
    f_color = texture(u_tex, v_uv);
}
"""

_VERT = """
#version 330
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_vert, 0.0, 1.0);
}
"""


def gpu_scale_readback(frame: np.ndarray, target_w: int, target_h: int,
                        *, ctx: Any = None) -> Optional[np.ndarray]:
    """GPU 双线性缩放到 (target_w, target_h) 并回读 RGBA。

    Args:
        frame: (H, W, 4) uint8 或 float32 RGBA。
        ctx: 可复用的 moderngl 上下文；None 时新建 standalone（离屏
            候选路径口径；生产路径由调用方传入共享上下文）。

    Returns:
        (target_h, target_w, 4) 同 dtype 数组；GPU 不可用返回 None。
    """
    if frame is None or frame.ndim != 3 or frame.shape[2] != 4 \
            or target_w <= 0 or target_h <= 0:
        return None
    owns_ctx = ctx is None
    try:
        if ctx is None:
            import moderngl
            ctx = moderngl.create_standalone_context()
    except Exception:  # noqa: BLE001 — GPU 不可用 → 调用方 CPU 回退
        return None
    # N16：资源逆序释放清单——共享 ctx 下失败路径也必须清理已建资源
    # （旧实现异常半途 tex/fbo/vao/vbo 泄漏在调用者的共享 ctx 上；
    # 成功路径 vbo 也从未释放）。
    created: list = []
    try:
        src_h, src_w = frame.shape[:2]
        dtype = frame.dtype
        tex_dtype = "f4" if dtype == np.float32 else "u1"
        tex = ctx.texture((src_w, src_h), 4,
                          np.ascontiguousarray(frame).tobytes(),
                          dtype=tex_dtype)
        created.append(tex)
        tex.use(0)
        prog = ctx.program(vertex_shader=_VERT, fragment_shader=_SCALE_FS)
        created.append(prog)
        prog["u_tex"].value = 0
        fbo = ctx.simple_framebuffer((target_w, target_h), components=4,
                                     dtype=tex_dtype)
        created.append(fbo)
        fbo.use()
        ctx.clear(0.0, 0.0, 0.0, 0.0)
        vbo = ctx.buffer(np.array(
            [-1, -1, 0, 0, 1, -1, 1, 0, -1, 1, 0, 1, 1, 1, 1, 1],
            dtype="f4").tobytes())
        created.append(vbo)
        vao = ctx.vertex_array(prog, vbo, "in_vert", "in_uv")
        created.append(vao)
        import moderngl as _mgl
        vao.render(_mgl.TRIANGLE_STRIP)
        raw = fbo.read(components=4, dtype=tex_dtype)
        arr = np.frombuffer(raw, dtype=dtype).reshape(target_h, target_w, 4)
        return arr
    except Exception:  # noqa: BLE001 — GPU 路径任何失败 → CPU 回退
        return None
    finally:
        # N16：逆序释放（vao → vbo → fbo → prog → tex），共享/自建 ctx
        # 一致（自建随 ctx.release 一并回收，逐项释放无害）。
        for res in reversed(created):
            try:
                res.release()
            except Exception:  # noqa: BLE001 — 释放尽力而为
                pass
        if owns_ctx:
            try:
                ctx.release()
            except Exception:
                pass


def cpu_resize_baseline(frame: np.ndarray, target_w: int, target_h: int
                        ) -> Optional[np.ndarray]:
    """现状基线：cv2.resize（INTER_LINEAR，与 GPU 采样器同族）。"""
    if frame is None:
        return None
    try:
        import cv2
        return cv2.resize(
            np.ascontiguousarray(frame),
            (int(target_w), int(target_h)),
            interpolation=cv2.INTER_LINEAR)
    except Exception:  # noqa: BLE001
        return None


def ab_metrics(source: np.ndarray, target_w: int, target_h: int,
               *, gpu: Optional[np.ndarray] = None,
               cpu: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """离屏 A/B 度量（与真实窗口复测同口径的候选路径数据）。

    Returns:
        {"full_frame_bytes", "target_bytes", "readback_ratio",
         "gpu_vs_cpu_max_abs", "gpu_available"}
    """
    src_h, src_w = source.shape[:2]
    # N16：按实际 dtype.itemsize 计字节（旧固定 ×4 只对 float32 成立，
    # uint8 帧被高估 4 倍——回读比随之失真）。
    itemsize = int(source.dtype.itemsize)
    full_bytes = int(src_w * src_h * 4 * itemsize)
    target_bytes = int(target_w * target_h * 4 * itemsize)
    result: Dict[str, Any] = {
        "full_frame_bytes": full_bytes,
        "target_bytes": target_bytes,
        "readback_ratio": round(target_bytes / float(max(1, full_bytes)), 4),
        "gpu_vs_cpu_max_abs": None,
        "gpu_available": gpu is not None,
    }
    if gpu is not None and cpu is not None \
            and gpu.shape == cpu.shape and gpu.dtype == cpu.dtype:
        g = gpu.astype(np.float32)
        c = cpu.astype(np.float32)
        result["gpu_vs_cpu_max_abs"] = float(np.max(np.abs(g - c)))
    return result


__all__ = ["gpu_scale_readback", "cpu_resize_baseline", "ab_metrics"]
