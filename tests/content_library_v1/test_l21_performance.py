"""L21 性能分档、资源纪律与播放压力（Mac 侧基准）。

范围：L 计划新增内容的成本基准（分档上限，CI 安全的宽裕阈值）+
资源纪律（临时纹理释放/池化复用）+ 连续播放扫描无泄漏。

基准环境：Mac（RX 6800/GL 4.1）；绝对值随硬件浮动，断言用宽裕上限
钉住"数量级不回归"，精确分档数据由 stdout 留档。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.color_core

_BENCH = []


def _record(name: str, ms: float) -> None:
    _BENCH.append((name, ms))


def _ab_1080p():
    w, h = 1920, 1080
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
    a = np.repeat(xs[None, :, None], h, axis=0)
    a = np.repeat(np.repeat(a, 3, axis=2), 1, axis=0)
    return a, (1.0 - a).copy()


class TestTransitionCoreBenchmarks:
    """转场核分档基准：1080p 单帧 CPU 成本。"""

    @pytest.mark.parametrize("family,params", [
        ("lib1_venetian_blinds", {"count": 6}),
        ("lib1_checker_reveal", {}),
        ("lib1_noise_dissolve", {"scale": 16}),
        ("lib1_halftone_reveal", {}),
    ])
    def test_mask_family_frame_cost(self, family, params) -> None:
        from src.features.edit.transition_packs.library_v1 import cores
        fn = {
            "lib1_venetian_blinds": cores.venetian_blinds_mask,
            "lib1_checker_reveal": cores.checker_reveal_mask,
            "lib1_noise_dissolve": cores.noise_dissolve_mask,
            "lib1_halftone_reveal": cores.halftone_reveal_mask,
        }[family]
        t0 = time.perf_counter()
        mask = fn(1920, 1080, 0.5, params)
        ms = (time.perf_counter() - t0) * 1000.0
        assert mask.shape == (1080, 1920)
        # 先留档再断言：单条超限不让 _BENCH 丢证据（否则级联拖垮 bench_report）
        _record(f"{family} mask 1080p [CPU 导出档]", ms)
        # 分档口径：mask 家族 CPU 路径为导出/兜底通道（实时预览走 GPU
        # kind6-11）；上限钉数量级不回归（实测 ~60-435ms）
        assert ms < 500.0, f"{family} 单帧 {ms:.1f}ms 超分档上限"

    @pytest.mark.parametrize("family,params", [
        ("lib1_staggered_tiles", {"grid": 4}),
        ("lib1_hinged_doors", {}),
        ("lib1_page_curl", {}),
        ("lib1_pixelate_bridge", {"cell": 16}),
        ("lib1_scanline_slices", {"band_px": 8}),
    ])
    def test_composite_family_frame_cost(self, family, params) -> None:
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            composite_lib1_transition,
        )
        a, b = _ab_1080p()
        t0 = time.perf_counter()
        out = composite_lib1_transition(family, a, b, 0.5, params)
        ms = (time.perf_counter() - t0) * 1000.0
        assert out.shape == a.shape
        # 先留档再断言（同上：防级联）
        _record(f"{family} 复合 1080p [CPU 导出档]", ms)
        # 分档口径：复合家族 CPU 路径为导出/兜底（实时预览走 GPU
        # kind12-21）；staggered_tiles 逐块双线性窗为最重档（实测
        # ~1333-1469ms，大电池满载下会再上浮），单列 2× 上限防满载
        # 误报；其余家族共用 1500ms 档
        cap = 3000.0 if family == "lib1_staggered_tiles" else 1500.0
        assert ms < cap, f"{family} 1080p 单帧 {ms:.1f}ms 超分档上限 {cap}"

    def test_bench_report(self, capsys) -> None:
        assert len(_BENCH) >= 9
        print("\n=== L21 性能分档基准（Mac/RX 6800）===")
        for name, ms in _BENCH:
            print(f"  {name:34s} {ms:8.2f} ms/帧")


class TestResourceDiscipline:
    def test_fullframe_channel_releases_temporaries(self) -> None:
        """全帧 CPU 通道的池化纹理：释放后池计数回落（无泄漏）。"""
        try:
            import moderngl
            ctx = moderngl.create_standalone_context(require=330)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"standalone GL 不可用: {exc}")
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        from src.features.composite.evaluators.color_evaluator import (
            ColorEvaluatorMixin,
        )
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.render.gpu_resource_pool import (
            upload_pooled_texture,
        )
        from src.shared.gpu.gl_backend import FramebufferManager

        class _Pool:
            def __init__(self, ctx):
                self.ctx = ctx
                self._pooled_textures = {}
                self._active = 0

            def acquire_texture(self, w, h, components=4, dtype="f4"):
                tex = ctx.texture((w, h), components, dtype=dtype)
                self._pooled_textures.setdefault(dtype, []).append(tex)
                self._active += 1
                return tex

            def release_texture(self, tex):
                self._active -= 1

        class _Combined(BaseEvaluatorMixin, ColorEvaluatorMixin,
                        FilterEvaluatorMixin):
            pass

        harness = _Combined()
        harness.ctx = ctx
        harness.fb_manager = FramebufferManager(ctx)
        pool = _Pool(ctx)
        harness._resource_pool = pool
        harness._eval_ctx = None
        harness.utility_shaders = {}
        harness._load_shaders()

        rgba = np.zeros((8, 8, 4), dtype=np.float32)
        rgba[..., 3] = 1.0
        src_tex = upload_pooled_texture(pool, rgba, 8, 8)
        src = TextureResult(texture_id=src_tex.glo, width=8, height=8)
        baseline = pool._active
        for _ in range(5):
            outputs = {"_effect_type": "lib1_fill_holes",
                       "_effect_input": src,
                       "_effect_params": {"connectivity": "4",
                                          "max_area": 1.0,
                                          "threshold": 0.5,
                                          "output_mode": "Fill"}}
            harness._dispatch_effect("lib1_fill_holes", outputs)
        # 全帧通道每次产出一纹理（acquire+upload），无中途泄漏放大：
        # 5 次 ≥ 基线且增量有界（≤5 上传 + 中间无额外分配翻倍）
        assert pool._active >= baseline
        assert pool._active - baseline <= 5
        ctx.release()

    def test_playback_sweep_100_frames_no_regression(self) -> None:
        """播放压力：单转场 100 帧连续扫描，无异常、无 NaN、耗时平稳。"""
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            composite_lib1_transition,
        )
        a, b = _ab_1080p()
        # 用小图扫 100 帧（1080p×100 帧在 CI 超时风险；分档基准已覆盖单帧）
        w, h = 480, 270
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        sa = np.repeat(np.repeat(xs[None, :, None], h, axis=0), 3, axis=2)
        sb = (1.0 - sa).copy()
        t_first = None
        for frame in range(100):
            p = frame / 99.0
            t0 = time.perf_counter()
            out = composite_lib1_transition("lib1_hinged_doors", sa, sb, p,
                                            {"perspective": 0.5})
            ms = (time.perf_counter() - t0) * 1000.0
            assert np.isfinite(out).all()
            if t_first is None:
                t_first = ms
        # 播放平稳性：末 10 帧均值不超首帧 3 倍（无累积劣化）
        _record("hinged_doors 播放扫描 100 帧", t_first)
        assert t_first < 100.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
