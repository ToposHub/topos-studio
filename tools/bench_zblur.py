#!/usr/bin/env python3
"""A3 ZBlur 性能基准 — 1080p 深度模糊（Vogel 盘采样 MVP）。

用法:
    .venv/bin/python tools/bench_zblur.py [--iterations 100]

口径:
  - moderngl standalone context，与测试 harness 同路径。
  - 验收门（计划 A3）：1080p 交互（draft/preview 档）< 8ms GPU。
    脚本同时测量 quality 档与 preview 减半采样两档。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np


def bench(harness, ctx, w, h, sample_count, iterations, warmup):
    img = np.zeros((h, w, 4), dtype=np.float32)
    img[h // 2 - 8:h // 2 + 8, w // 2 - 8:w // 2 + 8, :3] = 1.0
    img[..., 3] = 1.0
    depth = np.zeros((h, w, 4), dtype=np.float32)
    depth[..., 0] = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    depth[..., 3] = 1.0

    def upload(arr):
        tex = harness._resource_pool.acquire_texture(w, h)
        tex.write(np.ascontiguousarray(np.flipud(arr)).tobytes())
        from src.features.composite.nodes.base_node import TextureResult
        return TextureResult(texture_id=tex.glo, width=w, height=h,
                             alpha_premultiplied=False,
                             color_space="linear")

    src, dep = upload(img), upload(depth)
    params = {"depth_units": "normalized", "near": 0.0, "far": 100.0,
              "focus_distance": 1.0, "focal_range": 0.15,
              "max_blur_radius": 12.0, "sample_count": sample_count}

    for _ in range(warmup):
        harness._apply_depth_tool("zblur", src, dep, params, w, h)
    ctx.finish()

    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        harness._apply_depth_tool("zblur", src, dep, params, w, h)
        ctx.finish()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    args = parser.parse_args()

    import moderngl
    try:
        ctx = moderngl.create_standalone_context()
    except Exception as exc:
        print(f"Cannot create standalone GL context: {exc}")
        return 1

    from src.features.composite.evaluators.base_evaluator import (
        BaseEvaluatorMixin,
    )
    from src.features.composite.evaluators.color_evaluator import (
        ColorEvaluatorMixin,
    )
    from src.features.composite.evaluators.filter_evaluator import (
        FilterEvaluatorMixin,
    )

    class _Pool:
        def __init__(self, ctx):
            self.ctx = ctx
            self._pooled_textures = {}

        def acquire_texture(self, w, h, components=4, dtype="f4"):
            tex = self.ctx.texture((w, h), components, dtype=dtype)
            self._pooled_textures.setdefault(dtype, []).append(tex)
            return tex

    class _H(BaseEvaluatorMixin, ColorEvaluatorMixin, FilterEvaluatorMixin):
        def __init__(self, ctx):
            self.ctx = ctx
            self.utility_shaders = {}
            self._resource_pool = _Pool(ctx)

            from src.shared.gpu.gl_backend import FramebufferManager
            self.fb_manager = FramebufferManager(ctx)
            self._load_shaders()

        def _bind_texture_unit(self, texture_id, unit):
            tex = self._get_texture_by_id(texture_id)
            if tex is not None:
                tex.use(unit)

    harness = _H(ctx)
    w, h = args.width, args.height

    ok = True
    for label, count in (("draft/preview (8 samples)", 8),
                         ("normal (16 samples)", 16),
                         ("high (32 samples)", 32)):
        samples = bench(harness, ctx, w, h, count, args.iterations,
                        args.warmup)
        mean = statistics.fmean(samples)
        p95 = sorted(samples)[max(0, int(len(samples) * 0.95) - 1)]
        print(f"ZBlur {w}x{h} {label}: mean {mean:.2f} ms | "
              f"p95 {p95:.2f} ms")
        if count == 8 and mean >= 8.0:
            ok = False
    print(f"budget (draft <8ms GPU @1080p): {'PASS' if ok else 'FAIL'}")
    ctx.release()
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
