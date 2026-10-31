"""
T-03 GBR 平面直通端到端验证（standalone GL）。

用 Topos GBR 约定（planes = G,B,R 原始顺序 + gbr_mode=True）经过真实
YUVGPUUploader 上传与渲染，回读像素验证通道映射正确——覆盖本批修复的
"GBR 平面被按 YUV 矩阵渲染/错误换序" 缺陷。
"""

from __future__ import annotations

import numpy as np
import pytest

import moderngl

from src.shared.video.yuv_gpu_upload import YUVGPUUploader


W, H = 16, 16


@pytest.fixture(scope="module")
def gl_context():
    try:
        ctx = moderngl.create_standalone_context()
    except Exception as e:  # noqa: BLE001 - 无 GL 环境时跳过
        pytest.skip(f"standalone GL unavailable: {e}")
    yield ctx
    ctx.release()


@pytest.fixture
def uploader(gl_context):
    return YUVGPUUploader(gl_context, pool_size=2)


def _gbr_planes(g: int, b: int, r: int, bit_depth: int = 10) -> list[np.ndarray]:
    """Topos GBR 平面（G,B,R 顺序，uint16 满刻度码值）。"""
    max_code = (1 << bit_depth) - 1
    assert 0 <= g <= max_code and 0 <= b <= max_code and 0 <= r <= max_code
    return [
        np.full((H, W), g, dtype=np.uint16),
        np.full((H, W), b, dtype=np.uint16),
        np.full((H, W), r, dtype=np.uint16),
    ]


def _render_native_order_gbr(gl_context, up: YUVGPUUploader, planes, bit_depth):
    """复刻修复后调用方的路径：native 顺序上传 + gbr_mode=True。"""
    frame_idx = 0
    alpha_bit_depth = bit_depth
    texes = up.upload_yuv444(
        planes[0], planes[1], planes[2], frame_idx, bit_depth,
        a=None, alpha_bit_depth=alpha_bit_depth)
    fbo = gl_context.framebuffer(
        color_attachments=[gl_context.texture((W, H), 4, dtype="f1")])
    fbo.use()
    gl_context.viewport = (0, 0, W, H)
    gl_context.clear(0.0, 0.0, 0.0, 0.0)
    up.render_yuv_to_rgb(
        texes[0], texes[1], texes[2], fbo, bit_depth=bit_depth,
        a_tex=texes[3] if len(texes) >= 4 else None,
        alpha_bit_depth=alpha_bit_depth,
        gbr_mode=True)
    data = np.frombuffer(fbo.read(components=4), dtype=np.float32) \
        if False else np.frombuffer(fbo.read(components=4, dtype="f1"), dtype=np.uint8)
    img = data.reshape(H, W, 4)
    fbo.release()
    return img


class TestGbrPlanarPassthrough:
    def test_gbr_channel_mapping_native_order(self, gl_context, uploader):
        """G/B/R 常数平面 → 回读 RGB 通道一一对应（无 YUV 矩阵介入）。"""
        g_code, b_code, r_code = 400, 800, 600  # 10-bit 满刻度码值
        planes = _gbr_planes(g_code, b_code, r_code)
        img = _render_native_order_gbr(gl_context, uploader, planes, 10)

        # 满刻度归一化：code/1023 → 8bit
        expect_r = round(r_code / 1023.0 * 255.0)
        expect_g = round(g_code / 1023.0 * 255.0)
        expect_b = round(b_code / 1023.0 * 255.0)
        center = img[H // 2, W // 2]
        # 容差 ±2（shader 归一化舍入）
        assert abs(int(center[0]) - expect_r) <= 2, \
            f"R channel {center[0]} != {expect_r}"
        assert abs(int(center[1]) - expect_g) <= 2, \
            f"G channel {center[1]} != {expect_g}"
        assert abs(int(center[2]) - expect_b) <= 2, \
            f"B channel {center[2]} != {expect_b}"

    def test_gbr_distinct_planes_do_not_cross(self, gl_context, uploader):
        """三平面差异显著时通道互串即失败（回归旧缺陷的换序/矩阵渲染）。"""
        planes = _gbr_planes(g=100, b=900, r=500)
        img = _render_native_order_gbr(gl_context, uploader, planes, 10)
        center = img[H // 2, W // 2].astype(int)
        # 若走 YUV 矩阵或错误换序，通道值将显著偏离
        assert abs(center[0] - round(500 / 1023 * 255)) <= 2
        assert abs(center[1] - round(100 / 1023 * 255)) <= 2
        assert abs(center[2] - round(900 / 1023 * 255)) <= 2

    def test_upload_mode_r16ui_no_cpu_conversion(self, uploader):
        """GBR 上传后 last upload mode 为 r16ui_direct（无 CPU 归一化）。"""
        planes = _gbr_planes(400, 800, 600)
        uploader.upload_yuv444(
            planes[0], planes[1], planes[2], 0, 10,
            a=None, alpha_bit_depth=10)
        timing = uploader.get_last_upload_timing()
        assert timing.get("mode") == "r16ui_direct"
