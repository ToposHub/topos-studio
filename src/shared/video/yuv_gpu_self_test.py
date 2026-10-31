"""
macOS GPU YUV Self-Test Module

Purpose: Verify that the YUV -> RGB GPU conversion pipeline works correctly
on the current OpenGL context before enabling it for real playback.

Design:
- Creates small (4x4) synthetic YUV planes with known values
- Uploads them via YUVGPUUploader
- Renders through the YUV -> RGB shader to an FBO
- Reads back pixels and validates against CPU reference conversion
- Must run on the render thread (OpenGL context required)

Safety:
- Self-test failure does NOT affect playback — CPU fallback remains available
- Only runs once at renderer initialization, not per-frame
- readback is only done during self-test, never during playback
"""

from __future__ import annotations

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import List, Tuple

logger = logging.getLogger(__name__)


@dataclass
class YUVGpuSelfTestResult:
    """Result of the GPU YUV self-test."""
    passed: bool = False
    reason: str = ""
    format_tested: str = ""
    sample_values: List[Tuple[float, float, float]] = field(default_factory=list)
    expected_values: List[Tuple[float, float, float]] = field(default_factory=list)
    max_abs_error: float = 0.0
    avg_abs_error: float = 0.0

    def __str__(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        return (f"YUV GPU self-test {status}: format={self.format_tested}, "
                f"reason={self.reason}, max_error={self.max_abs_error:.4f}")


def _cpu_yuv_to_rgb_bt709_limited(
    y: float, u: float, v: float
) -> Tuple[float, float, float]:
    """
    CPU reference: YUV BT.709 limited range to float RGB [0, 1].

    Matches the GPU shader logic:
      y = (y - 16/255) * 255/219
      u = (u - 128/255) * 255/224
      v = (v - 128/255) * 255/224
      R = 1.0 * y + 0.0 * u + 1.5748 * v
      G = 1.0 * y + (-0.1873) * u + (-0.4681) * v
      B = 1.0 * y + 1.8556 * u + 0.0 * v
    """
    y_norm = (y - 16.0 / 255.0) * 255.0 / 219.0
    u_norm = (u - 128.0 / 255.0) * 255.0 / 224.0
    v_norm = (v - 128.0 / 255.0) * 255.0 / 224.0

    r = 1.0 * y_norm + 0.0 * u_norm + 1.5748 * v_norm
    g = 1.0 * y_norm + (-0.1873) * u_norm + (-0.4681) * v_norm
    b = 1.0 * y_norm + 1.8556 * u_norm + 0.0 * v_norm

    return (max(0.0, min(1.0, r)), max(0.0, min(1.0, g)), max(0.0, min(1.0, b)))


def _create_self_test_yuv444_8bit(
    width: int = 4, height: int = 4
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[Tuple[float, float, float, float, float, float]]]:
    """
    Create a 4x4 YUV444P 8-bit test frame with known values.

    Returns:
        (y_plane, u_plane, v_plane, test_points)
        test_points: [(y_uint8, u_uint8, v_uint8, expected_r, expected_g, expected_b), ...]
    """
    # Choose test values that exercise key points:
    # - Neutral gray (Y=128, U=128, V=128) → should produce ~mid gray
    # - White (Y=235, U=128, V=128) → should produce ~white (limited range)
    # - Red (Y=82, U=90, V=240) → classic red test
    # - Dark (Y=16, U=128, V=128) → should produce ~black (limited range)
    test_inputs = [
        (128, 128, 128),  # neutral
        (235, 128, 128),  # white (limited range)
        (82,  90,  240),  # red-ish
        (16,  128, 128),  # black (limited range)
        (210, 16,  146),  # green-ish
        (170, 166, 16),   # blue-ish
        (144, 54,  34),   # another color
        (41,  240, 110),  # another color
        (65,  100, 210),  # purple-ish
        (162, 44,  44),   # warm tone
        (200, 130, 130),  # near white
        (100, 128, 128),  # mid gray
        (180, 60,  180),  # magenta-ish
        (50,  200, 50),   # yellow-ish
        (120, 180, 30),   # teal-ish
        (90,  30,  170),  # another tone
    ]

    assert len(test_inputs) >= width * height, "Not enough test points for grid"

    y_plane = np.zeros((height, width), dtype=np.uint8)
    u_plane = np.zeros((height, width), dtype=np.uint8)
    v_plane = np.zeros((height, width), dtype=np.uint8)

    test_points = []
    for i, (yv, uv, vv) in enumerate(test_inputs[:width * height]):
        row = i // width
        col = i % width
        y_plane[row, col] = yv
        u_plane[row, col] = uv
        v_plane[row, col] = vv

        # Compute expected RGB
        yf = yv / 255.0
        uf = uv / 255.0
        vf = vv / 255.0
        er, eg, eb = _cpu_yuv_to_rgb_bt709_limited(yf, uf, vf)
        test_points.append((yf, uf, vf, er, eg, eb))

    return y_plane, u_plane, v_plane, test_points


def run_yuv_gpu_self_test(
    ctx,  # moderngl.Context
    uploader,  # YUVGPUUploader
) -> YUVGpuSelfTestResult:
    """
    Run the GPU YUV self-test.

    This MUST be called from the render thread with a valid ModernGL context.

    Args:
        ctx: ModernGL context (must be current)
        uploader: YUVGPUUploader instance

    Returns:
        YUVGpuSelfTestResult with pass/fail and diagnostic info
    """
    import moderngl

    result = YUVGpuSelfTestResult(format_tested="yuv444p_8bit_bt709_limited")

    # AUD-39720: output_tex/fbo 提前初始化为 None——外层 except 路径
    # （render/readback 抛异常）此前不清理这两个 GPU 资源，自检反复
    # 失败时每次泄漏一份 FBO + 4x4 纹理。
    output_tex = None
    fbo = None

    try:
        # P1.2: Create synthetic 4x4 YUV444 test data
        y_plane, u_plane, v_plane, test_points = _create_self_test_yuv444_8bit(4, 4)

        # P1.3: Upload via YUVGPUUploader
        try:
            y_tex, u_tex, v_tex = uploader.upload_yuv444(
                y_plane, u_plane, v_plane, frame_index=-999, bit_depth=8
            )
        except Exception as e:
            result.reason = f"upload_failed: {e}"
            logger.error(f"YUV GPU self-test: upload failed: {e}")
            return result

        # Verify textures were created
        if y_tex is None or u_tex is None or v_tex is None:
            result.reason = "upload_returned_none"
            logger.error("YUV GPU self-test: upload returned None texture(s)")
            return result

        # P1.4: Create output FBO and render YUV -> RGB
        fbo_w, fbo_h = 4, 4
        output_tex = ctx.texture((fbo_w, fbo_h), components=4, dtype='f1')
        output_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        output_tex.repeat_x = output_tex.repeat_y = False

        fbo = ctx.framebuffer(color_attachments=[output_tex])

        # Set BT.709 limited range on uploader
        uploader.set_yuv_matrix(0)  # BT.709
        uploader.set_color_range(0)  # Limited

        # Render
        fbo.use()
        ctx.viewport = (0, 0, fbo_w, fbo_h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)

        uploader.render_yuv_to_rgb(
            y_tex, u_tex, v_tex, fbo,
            chroma_uv_scale=(1.0, 1.0),
            chroma_uv_offset=(0.0, 0.0)
        )

        # P1.5: Readback and validate
        raw = fbo.read(viewport=(0, 0, fbo_w, fbo_h), components=4)
        pixels = np.frombuffer(raw, dtype=np.uint8).reshape(fbo_h, fbo_w, 4)

        # Check all pixels
        all_black = np.all(pixels[:, :, :3] == 0)
        all_transparent = np.all(pixels[:, :, 3] == 0)

        if all_black:
            result.reason = "readback-all-black"
            logger.error("YUV GPU self-test: readback returned all-black pixels")
            _cleanup_self_test_resources(fbo, output_tex)
            return result

        if all_transparent:
            result.reason = "readback-all-transparent"
            logger.error("YUV GPU self-test: readback returned all-transparent alpha")
            _cleanup_self_test_resources(fbo, output_tex)
            return result

        # Validate against CPU reference
        max_abs_error = 0.0
        total_abs_error = 0.0
        count = 0
        sample_gpu = []
        sample_expected = []

        for i, (yf, uf, vf, er, eg, eb) in enumerate(test_points):
            row = i // fbo_w
            col = i % fbo_w

            # ModernGL Framebuffer.read() returns rows in the same order used by
            # the synthetic upload/readback in this self-test.  Do not flip Y
            # here, otherwise the test compares each pixel against the wrong
            # source row and falsely disables the GPU YUV path on macOS.
            gr = pixels[row, col, 0] / 255.0
            gg = pixels[row, col, 1] / 255.0
            gb = pixels[row, col, 2] / 255.0

            dr = abs(gr - er)
            dg = abs(gg - eg)
            db = abs(gb - eb)
            pixel_error = max(dr, dg, db)

            max_abs_error = max(max_abs_error, pixel_error)
            total_abs_error += pixel_error
            count += 1

            sample_gpu.append((gr, gg, gb))
            sample_expected.append((er, eg, eb))

        result.sample_values = sample_gpu
        result.expected_values = sample_expected
        result.max_abs_error = max_abs_error
        result.avg_abs_error = total_abs_error / max(count, 1)

        # Cleanup
        _cleanup_self_test_resources(fbo, output_tex)

        # Validation threshold: allow tolerance for float16 precision loss
        # float16 upload + shader math can introduce ~2-3 LSB error at 8-bit
        ERROR_THRESHOLD = 0.03  # ~7.5/255 — generous for float16 roundtrip

        if max_abs_error <= ERROR_THRESHOLD:
            result.passed = True
            result.reason = "all_pixels_within_tolerance"
            logger.info(f"YUV GPU self-test PASSED: max_error={max_abs_error:.4f}, "
                       f"avg_error={result.avg_abs_error:.4f}")
        else:
            result.passed = False
            result.reason = f"color_error_too_high: max={max_abs_error:.4f}"
            logger.warning(f"YUV GPU self-test FAILED: max_error={max_abs_error:.4f} "
                          f"(threshold={ERROR_THRESHOLD})")

        return result

    except Exception as e:
        # AUD-39720: 异常路径必须清理已创建的 FBO/输出纹理（GL 资源
        # 只在本渲染线程内释放——自检本身即运行在渲染线程）。
        _cleanup_self_test_resources(fbo, output_tex)
        result.reason = f"exception: {e}"
        logger.error(f"YUV GPU self-test exception: {e}", exc_info=True)
        return result


def _cleanup_self_test_resources(fbo, tex) -> None:
    """Clean up self-test resources."""
    try:
        if fbo is not None:
            fbo.release()
    except Exception:
        pass
    try:
        if tex is not None:
            tex.release()
    except Exception:
        pass


def run_r16ui_gpu_self_test(
    ctx,
    uploader,
) -> YUVGpuSelfTestResult:
    """
    Run the R16UI 12-bit YUV self-test.

    Tests that usampler2D integer texture sampling produces correct colors
    for 12-bit YUV444P data through the full GPU pipeline:
    uint16 planes → R16UI textures → usampler2D shader → RGB output.

    This MUST be called from the render thread with a valid ModernGL context.

    Args:
        ctx: ModernGL context (must be current)
        uploader: YUVGPUUploader instance

    Returns:
        YUVGpuSelfTestResult with pass/fail and diagnostic info
    """

    result = YUVGpuSelfTestResult(format_tested="yuv444p_12bit_r16ui_bt709_limited")

    try:
        # Use uploader's built-in R16UI self-test
        passed = uploader.run_r16ui_self_test()

        if passed:
            result.passed = True
            result.reason = "all_pixels_within_tolerance"
            logger.info("R16UI YUV GPU self-test PASSED")
        else:
            result.passed = False
            reason = getattr(uploader, '_r16ui_disabled_reason', 'unknown')
            result.reason = f"r16ui_self_test_failed: {reason}"
            logger.warning(f"R16UI YUV GPU self-test FAILED: {reason}")

        return result

    except Exception as e:
        result.reason = f"exception: {e}"
        logger.error(f"R16UI YUV GPU self-test exception: {e}", exc_info=True)
        return result
