"""MSL blur / erode-dilate kernel 数值奇偶校验测试（计划 P4-02）。

对照 `kernels.py` 的 numpy 参考实现（与 GLSL `_COMPUTE_SOURCES` 语义
逐条对齐），验收「画质与 fragment 路径一致」。真机层，无 Metal 设备
时整类跳过。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.shared.gpu.metal import MetalComputeContext
from src.shared.gpu.metal.context import MetalComputeContext as _Ctx
from src.shared.gpu.metal.kernels import (
    METAL_COMPUTE_SOURCES,
    erode_dilate_reference,
    gaussian_blur_reference,
    make_blur_params,
    make_erode_dilate_params,
)

requires_metal = pytest.mark.skipif(
    not _Ctx.is_supported(), reason="无 Metal 设备"
)

ATOL = 2e-3  # float16 输出的 1-2 ulp 容差（kernel 与参考都在 fp32 累加）


def _random_image(h: int, w: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.random((h, w, 4)) * 4.0).astype(np.float16)


def _run_kernel(ctx: MetalComputeContext, name: str, img: np.ndarray,
                params: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    src = ctx.create_buffer_from_numpy(img)
    dst = ctx.create_buffer(img.nbytes)
    param_buf = ctx.create_buffer_from_numpy(params)
    kernel = ctx.compile_kernel(name, METAL_COMPUTE_SOURCES[name])
    if name == "blur_h_shared":
        # 沿 X 铺组：每组 64 线程处理一行的 64 列
        threads = (((w + 63) // 64) * 64, h, 1)
        group = (64, 1, 1)
    elif name == "blur_v_shared":
        # 沿 Y 铺组：每组 64 线程处理一列的 64 行
        threads = (w, ((h + 63) // 64) * 64, 1)
        group = (1, 64, 1)
    else:
        threads = (w, h, 1)
        group = None
    ctx.dispatch_sync(
        kernel, buffers={0: src, 1: dst, 2: param_buf},
        threads=threads, group=group,
    )
    return np.frombuffer(ctx.read_buffer(dst), dtype=np.float16).reshape(h, w, 4)


@requires_metal
class TestGaussianBlurParity:
    @pytest.mark.parametrize("radius", [4.0, 16.0, 48.0])
    @pytest.mark.parametrize("edge_policy", [0, 1])
    def test_blur_h_matches_reference(self, radius, edge_policy):
        img = _random_image(23, 37)
        ctx = MetalComputeContext()
        out = _run_kernel(
            ctx, "blur_h", img,
            make_blur_params(37, 23, radius, edge_policy),
        )
        ref = gaussian_blur_reference(img, radius, axis=0, edge_policy=edge_policy)
        np.testing.assert_allclose(out.astype(np.float32), ref.astype(np.float32),
                                   atol=ATOL)

    @pytest.mark.parametrize("radius", [4.0, 48.0])
    def test_blur_v_matches_reference(self, radius):
        img = _random_image(19, 41)
        ctx = MetalComputeContext()
        out = _run_kernel(ctx, "blur_v", img, make_blur_params(41, 19, radius))
        ref = gaussian_blur_reference(img, radius, axis=1)
        np.testing.assert_allclose(out.astype(np.float32), ref.astype(np.float32),
                                   atol=ATOL)

    def test_kernel_size_cap_at_64(self):
        """半径 200 → σ=100 → kernel_size=min(300,64)=64，与参考一致。"""
        img = _random_image(16, 16, seed=11)
        ctx = MetalComputeContext()
        out = _run_kernel(ctx, "blur_h", img, make_blur_params(16, 16, 200.0))
        ref = gaussian_blur_reference(img, 200.0, axis=0)
        np.testing.assert_allclose(out.astype(np.float32), ref.astype(np.float32),
                                   atol=ATOL)

    @pytest.mark.parametrize("name", ["blur_h_shared", "blur_v_shared"])
    def test_shared_tile_variant_matches_simple(self, name):
        """tile 变体与 simple 变体画质一致（验收：同名同语义）。"""
        img = _random_image(32, 100, seed=13)
        radius, policy = 48.0, 0
        ctx = MetalComputeContext()
        out = _run_kernel(ctx, name, img, make_blur_params(100, 32, radius, policy))
        simple = "blur_h" if name == "blur_h_shared" else "blur_v"
        ref = _run_kernel(ctx, simple, img,
                          make_blur_params(100, 32, radius, policy))
        np.testing.assert_allclose(out.astype(np.float32), ref.astype(np.float32),
                                   atol=ATOL)


@requires_metal
class TestErodeDilateParity:
    @pytest.mark.parametrize("operation", ["erode", "dilate"])
    @pytest.mark.parametrize("channels", [0, 1])
    @pytest.mark.parametrize("size", [1, 4, 5])
    def test_morphology_matches_reference(self, operation, channels, size):
        img = _random_image(21, 29, seed=17)
        ctx = MetalComputeContext()
        out = _run_kernel(
            ctx, "erode_dilate", img,
            make_erode_dilate_params(29, 21, operation, size, channels),
        )
        ref = erode_dilate_reference(img, operation, size, channels)
        np.testing.assert_allclose(out.astype(np.float32), ref.astype(np.float32),
                                   atol=ATOL)

    def test_shared_variant_matches_simple(self):
        img = _random_image(24, 30, seed=19)
        ctx = MetalComputeContext()
        params = make_erode_dilate_params(30, 24, "dilate", 5, 1)
        out = _run_kernel(ctx, "erode_dilate_shared", img, params)
        ref = _run_kernel(ctx, "erode_dilate", img, params)
        np.testing.assert_allclose(out.astype(np.float32), ref.astype(np.float32),
                                   atol=ATOL)


@requires_metal
class TestSourceCoverage:
    def test_kernel_names_align_with_compute_shader_manager(self):
        """P4-02 覆盖率验收：6 个大半径效果名全部可编译。"""
        from src.features.composite.render.compute_shader_manager import (
            ComputeShaderManager,
        )
        expected = set(ComputeShaderManager._COMPUTE_SOURCES) - {"histogram"}
        assert set(METAL_COMPUTE_SOURCES) == expected
        ctx = MetalComputeContext()
        for name, source in METAL_COMPUTE_SOURCES.items():
            kernel = ctx.compile_kernel(name, source)
            assert kernel.max_threads_per_group > 0
