#!/usr/bin/env python3
"""A1 Relight 节点性能基准 — 1080p 逐像素重光照 shader。

用法:
    .venv/bin/python tools/bench_relight.py [--width 1920 --height 1080]
                                            [--iterations 200]

口径:
  - moderngl standalone context（无 Qt 依赖），与测试 harness 同路径。
  - 每次迭代完整执行 _apply_relight（三纹理绑定 + 全屏 quad），
    ctx.finish() 同步 GPU 后计时。
  - 验收门（计划 A1）：1080p 交互帧耗 < 2ms GPU。
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
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

            self._bind_external_texture = self._bind_external_texture

        def _bind_external_texture(self, tex_id):
            self._get_texture_by_id(tex_id).use(0)

        def _bind_texture_unit(self, texture_id, unit):
            tex = self._get_texture_by_id(texture_id)
            if tex is not None:
                tex.use(unit)

    w, h = args.width, args.height
    harness = _H(ctx)

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([xx / w, yy / h, np.full_like(xx, 0.5),
                    np.ones_like(xx)], axis=-1).astype(np.float32)
    pos = np.zeros((h, w, 4), dtype=np.float32)
    pos[..., 0] = 10.0 * xx / (w - 1)
    pos[..., 1] = 7.5 * yy / (h - 1)
    pos[..., 3] = 1.0
    nrm = np.zeros((h, w, 4), dtype=np.float32)
    nrm[..., 0] = 0.5
    nrm[..., 1] = 0.5
    nrm[..., 2] = 1.0
    nrm[..., 3] = 1.0

    def upload(arr):
        tex = harness._resource_pool.acquire_texture(w, h)
        tex.write(np.ascontiguousarray(np.flipud(arr)).tobytes())
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.core.format import BBox
        return TextureResult(texture_id=tex.glo, width=w, height=h,
                             bbox=BBox.full_frame(w, h),
                             alpha_premultiplied=False,
                             color_space="linear")

    src, nrm_tex, pos_tex = upload(img), upload(nrm), upload(pos)
    params = {"light_type": "point", "light_position": (10.0, 0.0, 5.0),
              "intensity": 1.2, "ambient": 0.05, "color": (1.0, 0.9, 0.8),
              "normal_decode": "encoded", "normal_flip_y": False}

    for _ in range(args.warmup):
        harness._apply_relight(src, nrm_tex, pos_tex, params, w, h)
    ctx.finish()

    samples = []
    for _ in range(args.iterations):
        t0 = time.perf_counter()
        harness._apply_relight(src, nrm_tex, pos_tex, params, w, h)
        ctx.finish()
        samples.append((time.perf_counter() - t0) * 1000.0)

    mean = statistics.fmean(samples)
    p95 = sorted(samples)[max(0, int(len(samples) * 0.95) - 1)]
    budget_ok = mean < 2.0
    print(f"Relight {w}x{h} point light, {args.iterations} iters")
    print(f"  mean {mean:.3f} ms | p95 {p95:.3f} ms | "
          f"min {min(samples):.3f} ms")
    print(f"  budget (<2ms GPU @1080p): "
          f"{'PASS' if budget_ok else 'FAIL'}")
    ctx.release()
    return 0 if budget_ok else 2


if __name__ == "__main__":
    sys.exit(main())
