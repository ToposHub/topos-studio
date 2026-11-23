"""迁移计划「参考实现 4A · Metal 纹理和遮罩的最小接入」验证（阶段 4.2 前置）。

验收口径（计划原文）：三个像素的遮罩 ``[0.0, 0.5, 1.0]``、相同输入
``(-0.25, 0.5, 2.0, 0.5)``、``gain = 2.0``——预期 RGB 依次为
``(-0.25, 0.5, 2.0)``、``(-0.375, 0.75, 3.0)``、``(-0.5, 1.0, 4.0)``，
Alpha 均为 ``0.5``（**位级相等**——fp16 可精确表示这些值）。再验证
尺寸不整除线程组、两次提交乱序完成和透传旁路。

真机层（无 Metal 设备整类跳过 = NOT RUN）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.shared.gpu.metal.context import MetalComputeContext
from src.features.color.core.metal_texture_kernels import (
    METAL_REFERENCE_4A_MSL,
)

requires_metal = pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="无 Metal 设备（NOT RUN）")


def _pixel_setup(ctx: MetalComputeContext):
    import Metal

    rgba = np.tile(np.array([-0.25, 0.5, 2.0, 0.5], dtype=np.float16),
                   (1, 3, 1))
    mask = np.array([[0.0, 0.5, 1.0]], dtype=np.float16)
    source = ctx.create_texture_from_numpy(
        rgba, Metal.MTLPixelFormatRGBA16Float, 3, 1)
    matte = ctx.create_texture_from_numpy(
        mask, Metal.MTLPixelFormatR16Float, 3, 1)
    output = ctx.create_texture_2d(
        Metal.MTLPixelFormatRGBA16Float, 3, 1, shader_write=True)
    gain = ctx.create_buffer_from_numpy(np.array([2.0], dtype=np.float32))
    return source, matte, output, gain


@pytest.fixture(scope="module")
def _ctx():
    ctx = MetalComputeContext()
    yield ctx
    ctx.finish()


@pytest.fixture(scope="module")
def _kernel(_ctx):
    return _ctx.compile_kernel("test_linear_gain_with_matte",
                               METAL_REFERENCE_4A_MSL)


@requires_metal
class TestReference4A:
    @pytest.fixture()
    def ctx(self, _ctx):
        return _ctx

    @pytest.fixture()
    def kernel(self, _kernel):
        return _kernel

    def test_pixel_expectations_from_plan(self, ctx, kernel):
        """计划期望值：位级相等（fp16 精确表示所有 involved 值）。"""
        source, matte, output, gain = _pixel_setup(ctx)
        ctx.dispatch(kernel, buffers={0: gain},
                     textures={0: source, 1: matte, 2: output},
                     threads=(3, 1, 1))
        ctx.submit(wait=True)
        got = ctx.read_texture(output, 12, 1, dtype=np.float16,
                               region_width=3).reshape(3, 4)
        expected = np.array([
            [-0.25, 0.5, 2.0, 0.5],
            [-0.375, 0.75, 3.0, 0.5],
            [-0.5, 1.0, 4.0, 0.5],
        ], dtype=np.float16)
        np.testing.assert_array_equal(got, expected)

    def test_passthrough_bypass(self, ctx, kernel):
        """coverage=0 的像素原样透传（gain 不生效、Alpha 不动）。"""
        import Metal

        rgba = np.tile(np.array([0.25, -0.5, 4.0, 0.75], dtype=np.float16),
                       (1, 4, 1))
        mask = np.zeros((1, 4), dtype=np.float16)
        source = ctx.create_texture_from_numpy(
            rgba, Metal.MTLPixelFormatRGBA16Float, 4, 1)
        matte = ctx.create_texture_from_numpy(
            mask, Metal.MTLPixelFormatR16Float, 4, 1)
        output = ctx.create_texture_2d(
            Metal.MTLPixelFormatRGBA16Float, 4, 1, shader_write=True)
        gain = ctx.create_buffer_from_numpy(np.array([9.0], dtype=np.float32))
        ctx.dispatch(kernel, buffers={0: gain},
                     textures={0: source, 1: matte, 2: output},
                     threads=(4, 1, 1))
        ctx.submit(wait=True)
        got = ctx.read_texture(output, 16, 1, dtype=np.float16,
                               region_width=4).reshape(4, 4)
        np.testing.assert_array_equal(
            got, rgba.reshape(4, 4).astype(np.float16))

    def test_size_not_divisible_by_threadgroup(self, ctx, kernel):
        """65×33（16×16 组不整除）：逐像素等于 numpy 参考（含 HDR/负值）。"""
        import Metal

        rng = np.random.default_rng(41)
        w, h = 65, 33
        rgba = np.clip(rng.uniform(-0.5, 4.0, (h, w, 4)),
                       -0.5, 8.0).astype(np.float16)
        mask = rng.uniform(0.0, 1.0, (h, w)).astype(np.float16)
        source = ctx.create_texture_from_numpy(
            rgba, Metal.MTLPixelFormatRGBA16Float, w, h)
        matte = ctx.create_texture_from_numpy(
            mask, Metal.MTLPixelFormatR16Float, w, h)
        output = ctx.create_texture_2d(
            Metal.MTLPixelFormatRGBA16Float, w, h, shader_write=True)
        gain = ctx.create_buffer_from_numpy(np.array([2.0], dtype=np.float32))
        ctx.dispatch(kernel, buffers={0: gain},
                     textures={0: source, 1: matte, 2: output},
                     threads=(w, h, 1))
        ctx.submit(wait=True)
        got = ctx.read_texture(output, w * 4, h, dtype=np.float16,
                               region_width=w).reshape(h, w, 4)
        coverage = np.clip(mask.astype(np.float32), 0.0, 1.0)[..., None]
        src32 = rgba.astype(np.float32)
        expected = src32.copy()
        expected[..., :3] = (src32[..., :3] * (1.0 - coverage)
                             + src32[..., :3] * 2.0 * coverage)
        # fp16 写出 + GPU FMA 收缩：与 numpy 参考允许 ±1 fp16 ulp——按
        # 仓库 G2 双容差口径（SDR ≤1/255 abs；|x|>1 HDR ≤2e-3 rel）
        expected = expected.astype(np.float16).astype(np.float32)
        got32 = got.astype(np.float32)
        diff = np.abs(got32 - expected)
        sdr = np.abs(expected) <= 1.0
        assert diff[sdr].max(initial=0.0) <= 1.0 / 255.0, (
            f"SDR max diff {diff[sdr].max(initial=0.0):.3e}")
        mag = np.maximum(np.abs(expected), 1e-6)
        rel = (diff[~sdr] / mag[~sdr]).max(initial=0.0)
        assert rel <= 2e-3, f"HDR rel {rel:.3e}"

    def test_two_submissions_out_of_order_completion(self, ctx, kernel):
        """两次异步提交（不等待）各自写入独立输出：串行队列下无串扰。"""
        import Metal

        w, h = 16, 16
        base = np.full((h, w, 4), (0.5, 0.5, 0.5, 1.0), dtype=np.float16)
        white = np.ones((h, w), dtype=np.float16)
        source = ctx.create_texture_from_numpy(
            base, Metal.MTLPixelFormatRGBA16Float, w, h)
        matte = ctx.create_texture_from_numpy(
            white, Metal.MTLPixelFormatR16Float, w, h)
        out_a = ctx.create_texture_2d(
            Metal.MTLPixelFormatRGBA16Float, w, h, shader_write=True)
        out_b = ctx.create_texture_2d(
            Metal.MTLPixelFormatRGBA16Float, w, h, shader_write=True)
        gain_a = ctx.create_buffer_from_numpy(
            np.array([2.0], dtype=np.float32))
        gain_b = ctx.create_buffer_from_numpy(
            np.array([4.0], dtype=np.float32))
        # 两次独立编码-提交（wait=False），随后统一读回
        ctx.dispatch(kernel, buffers={0: gain_a},
                     textures={0: source, 1: matte, 2: out_a},
                     threads=(w, h, 1))
        ctx.submit(wait=False)
        ctx.dispatch(kernel, buffers={0: gain_b},
                     textures={0: source, 1: matte, 2: out_b},
                     threads=(w, h, 1))
        ctx.submit(wait=False)
        ctx.finish()
        got_a = ctx.read_texture(out_a, w * 4, h, dtype=np.float16,
                                 region_width=w).reshape(h, w, 4)
        got_b = ctx.read_texture(out_b, w * 4, h, dtype=np.float16,
                                 region_width=w).reshape(h, w, 4)
        np.testing.assert_allclose(got_a[..., 0], 1.0, atol=1e-3)
        np.testing.assert_allclose(got_b[..., 0], 2.0, atol=1e-3)
        np.testing.assert_allclose(got_a[..., 3], 1.0, atol=1e-6)
