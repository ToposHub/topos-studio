"""GPU→CPU 读回微基准：定位 4K comp_clip 读回 ~500ms 的根因。

在 standalone GL 上下文（默认 3.3 与显式 4.6 各一次）对 3840×2160 FBO 测：
  - fbo.read() 各格式（u1 3/4 通道、f4 4 通道）一次性分配 vs read_into 预分配
  - 纹理 .read() 对照
  - RGBA8 附件 vs f4 附件对读回速度的影响

每项 warmup 2 次后取 5 次中位（毫秒）。
"""

from __future__ import annotations

import statistics
import time

import moderngl
import numpy as np

W, H = 3840, 2160


def bench(label: str, fn, n: int = 5, warmup: int = 2) -> None:
    for _ in range(warmup):
        fn()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000.0)
    print(f"  {label:46s} {statistics.median(ts):9.1f} ms (min {min(ts):.1f})", flush=True)


def run(require: int | None) -> None:
    tag = f"require={require}" if require else "default(3.3)"
    print(f"\n=== GL {tag} @ {W}x{H} ===", flush=True)
    ctx = moderngl.create_standalone_context(require=require) if require \
        else moderngl.create_standalone_context()
    print(f"  context: {ctx.info['GL_VERSION']} | {ctx.info['GL_RENDERER']}", flush=True)

    tex_f4 = ctx.texture((W, H), 4, dtype="f4")
    tex_u1 = ctx.texture((W, H), 4, dtype="u1")
    fbo_f4 = ctx.framebuffer(color_attachments=[tex_f4])
    fbo_u1 = ctx.framebuffer(color_attachments=[tex_u1])
    for f in (fbo_f4, fbo_u1):
        f.use()
        ctx.viewport = (0, 0, W, H)
        ctx.clear(0.5, 0.25, 0.75, 1.0)

    buf_u1 = np.empty(W * H * 4, dtype="u1")
    buf_f4 = np.empty(W * H * 4, dtype="f4")

    print(" -- RGBA8 附件（srgb_u8 预览路径形态）--")
    bench("fbo_u1.read components=3 u1", lambda: fbo_u1.read(components=3))
    bench("fbo_u1.read components=4 u1", lambda: fbo_u1.read(components=4))
    bench("fbo_u1.read_into u1 (预分配)", lambda: fbo_u1.read_into(buf_u1, components=4))

    print(" -- f4 附件（linear_f32 导出路径形态）--")
    bench("fbo_f4.read components=4 f4", lambda: fbo_f4.read(components=4, dtype="f4"))
    bench("fbo_f4.read_into f4 (预分配)", lambda: fbo_f4.read_into(buf_f4, components=4, dtype="f4"))
    bench("fbo_f4.read_into -> PBO buffer", lambda: fbo_f4.read_into(
        ctx.buffer(reserve=W * H * 16), components=4, dtype="f4"))
    bench("texture_f4.read()", lambda: tex_f4.read())

    for t in (tex_f4, tex_u1):
        t.release()
    for f in (fbo_f4, fbo_u1):
        f.release()
    ctx.release()


if __name__ == "__main__":
    run(None)
    run(460)
