"""阶段 7：Alpha 与 HDR 渲染链测试（decode → GPU → composite 一致性）。

覆盖计划 §12 阶段 7 门禁：
- YUVA 第四平面 GPU 上传（float16 / R16UI / R8 三路径）；
- shader alpha 采样 + Alpha 位深独立归一化（a8/a16 vs luma 10）；
- straight / premultiplied 显式处理（预乘源在 YUV→RGB 边界转 straight）；
- 硬 Alpha 边缘 straight 合成无黑边/白边；
- CPU fallback 不再把 10/16-bit Alpha 降为 8-bit（f2 路径 + 正确位深）；
- 格式分类（topos_yuva*/yuva* → 对应子采样类）；
- 池槽位隔离与 release 清理；
- Topos 解码 → GPU 读回 alpha 与解码平面一致（round-trip 门禁核心）。

GL 用例需 standalone ModernGL 上下文（gpu_renderer 标记，无上下文时
skip-with-reason）；素材用例需 libtopos_codec（未构建时 skip）。
"""
from __future__ import annotations

import ctypes

import numpy as np
import pytest

pytestmark = pytest.mark.media_core

from src.shared.codec import topos_binding
from src.shared.codec.topos_binding import (
    TOPOS_CODEC_ABI_VERSION,
    ToposCodec,
    ToposMuxFile,
)
from src.shared.video.yuv_gpu_upload import YUVGPUUploader

# 素材参数（与 test_topos_source.py 同规格：64×40 4:2:2、5 帧、24fps）
W, H, N = 64, 40, 5
TIMESCALE, FRAME_DUR = 24000, 1000

try:
    import moderngl
    HAS_MODERNGL = True
except ImportError:
    HAS_MODERNGL = False


# =============================================================================
# Fixtures
# =============================================================================


def _lib_available() -> bool:
    import os

    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is not None:
        return True
    return any(p.is_file() for p in topos_binding.candidate_library_paths())


@pytest.fixture(scope="module")
def codec() -> ToposCodec:
    if not _lib_available():
        pytest.skip("libtopos_codec 未构建；先运行 bash native/topos_codec/run_tests.sh")
    return ToposCodec()


@pytest.fixture
def gl_context():
    if not HAS_MODERNGL:
        pytest.skip("moderngl not available")
    try:
        ctx = moderngl.create_standalone_context()
    except Exception as e:
        pytest.skip(f"Failed to create OpenGL context: {e}")
    yield ctx
    ctx.release()


@pytest.fixture
def uploader(gl_context):
    up = YUVGPUUploader(gl_context, pool_size=4)
    yield up
    up.release()


def _frame_config(
    w: int,
    h: int,
    qp: int = 20,
    alpha_mode: int = 0,
    alpha_bit_depth: int = 16,
) -> topos_binding._CFrameConfig:
    c = topos_binding._CFrameConfig()
    c.struct_size = ctypes.sizeof(topos_binding._CFrameConfig)
    c.abi_version = TOPOS_CODEC_ABI_VERSION
    c.visible_width, c.visible_height = w, h
    c.qp_base = qp
    c.qmatrix_id = 1
    c.alpha_mode = alpha_mode
    c.alpha_bit_depth = alpha_bit_depth if alpha_mode else 0
    c.color_range = 1
    c.color_primaries = 1
    c.color_transfer = 1
    c.color_matrix = 1
    c.sar_num = 1
    c.sar_den = 1
    return c


def _alpha_movie(
    codec: ToposCodec,
    path: str,
    *,
    alpha_bit_depth: int,
) -> None:
    """生成 mode2（side plane）Topos MOV。

    Y/U/V 用中性灰梯度（full range），Alpha 用满量程行 Ramp——
    alpha 码值严格落在 0..2^N-1 内，供 GPU 读回精确比对。
    """
    fc = _frame_config(W, H, qp=12, alpha_mode=2, alpha_bit_depth=alpha_bit_depth)
    mc = codec.movie_config(
        W, H, qp=12, qmatrix=1,
        alpha_mode=2, alpha_bit_depth=alpha_bit_depth,
    )
    a_max = (1 << alpha_bit_depth) - 1
    with ToposMuxFile(codec, path, mc) as mux:
        for i in range(N):
            # full-range 中性灰：Y 恒 512、U/V 恒 512（10-bit 中点）
            y = np.full((H, W), 512, "<u2")
            c = np.full((H, (W + 1) // 2), 512, "<u2")
            # alpha 行方向 ramp（帧间加偏移，覆盖不同码值段）
            a = (
                (np.arange(W)[None, :] * a_max // max(1, W - 1) + i * 17)
                % (a_max + 1)
            ).astype("<u2")
            a = np.repeat(a, H, axis=0)
            pkt, _ = codec.encode_frame(fc, [
                y.tobytes(), c.tobytes(), c.tobytes(), a.tobytes(),
            ])
            mux.add_packet(pkt, pts=i * FRAME_DUR, dur=FRAME_DUR)
        mux.finish()


def _neutral_gray_planes() -> tuple:
    """full-range 中性灰 YUV422 10-bit + 行 ramp alpha（直接喂 GPU 用）。"""
    y = np.full((H, W), 512, "<u2")
    c = np.full((H, (W + 1) // 2), 512, "<u2")
    a10 = (np.arange(W)[None, :] * 1023 // (W - 1)).astype("<u2")
    a10 = np.repeat(a10, H, axis=0)
    return y, c, c, a10


def _render_and_read(
    ctx: "moderngl.Context",
    uploader: YUVGPUUploader,
    texes: tuple,
    *,
    bit_depth: int,
    alpha_bit_depth: int,
    alpha_premultiplied: bool = False,
    force_r16ui: bool = False,
) -> np.ndarray:
    """渲染平面到 RGBA16F FBO 并读回 (H, W, 4) float16（方向与输入一致）。"""
    a_tex = texes[3] if len(texes) >= 4 else None
    out = ctx.texture((W, H), components=4, dtype="f2")
    out.filter = (moderngl.NEAREST, moderngl.NEAREST)
    out.repeat_x = out.repeat_y = False
    fbo = ctx.framebuffer(color_attachments=[out])
    fbo.use()
    ctx.viewport = (0, 0, W, H)
    ctx.clear(0.0, 0.0, 0.0, 0.0)

    if force_r16ui or uploader._last_upload_mode == "r16ui_direct":
        uploader.render_yuv_r16ui_to_rgb(
            texes[0], texes[1], texes[2], fbo,
            bit_depth=bit_depth,
            a_tex=a_tex,
            alpha_bit_depth=alpha_bit_depth,
            alpha_premultiplied=alpha_premultiplied,
        )
    else:
        uploader.render_yuv_to_rgb(
            texes[0], texes[1], texes[2], fbo,
            bit_depth=bit_depth,
            a_tex=a_tex,
            alpha_bit_depth=alpha_bit_depth,
            alpha_premultiplied=alpha_premultiplied,
        )
    raw = fbo.read(viewport=(0, 0, W, H), components=4, dtype="f2")
    rgba = np.frombuffer(raw, dtype=np.float16).reshape(H, W, 4)
    fbo.release()
    out.release()
    return rgba


def _disable_r16ui(uploader: YUVGPUUploader) -> None:
    """强制 float16 路径（覆盖 R16UI 不可用时的通用回退）。"""
    uploader._r16ui_self_test_passed = False
    uploader._r16ui_auto_tested = True
    uploader._r16ui_pool = None


def _r16ui_ok(uploader: YUVGPUUploader) -> bool:
    return uploader._r16ui_probe_ok and uploader.run_r16ui_self_test()


# =============================================================================
# 分类回归（无 GL）
# =============================================================================


class TestFormatClassification:
    def test_yuva_and_topos_names_route_by_subsampling(self) -> None:
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _classify_planar_format,
        )

        assert _classify_planar_format("topos_yuva422p10a8", 4, 10) == "yuv422"
        assert _classify_planar_format("topos_yuva422p10a10", 4, 10) == "yuv422"
        assert _classify_planar_format("topos_yuva422p10a12", 4, 10) == "yuv422"
        assert _classify_planar_format("topos_yuva422p10a16", 4, 10) == "yuv422"
        assert _classify_planar_format("yuva422p", 4, 8) == "yuv422"
        assert _classify_planar_format("yuva422p10le", 4, 10) == "yuv422"
        assert _classify_planar_format("yuva444p10le", 4, 10) == "yuv444"
        assert _classify_planar_format("yuva420p", 4, 8) == "yuv420"
        # R4.6：4:4:4/GBR topos 名路由（444 → yuv444；GBR → rgb_planar 恒等）
        assert _classify_planar_format("topos_yuva444p10a8", 4, 10) == "yuv444"
        assert _classify_planar_format("topos_yuva444p12a16", 4, 12) == "yuv444"
        assert _classify_planar_format("topos_gbrap10a8", 4, 10) == "rgb_planar"
        assert _classify_planar_format("topos_gbrap12a16", 4, 12) == "rgb_planar"
        assert _classify_planar_format("gbrp12le", 3, 12) == "rgb_planar"
        # 无 Alpha 的 Topos 名复用 yuv422p10le（阶段 6 决策不变）
        assert _classify_planar_format("yuv422p10le", 3, 10) == "yuv422"


# =============================================================================
# 池槽位与释放（GL）
# =============================================================================


class TestAlphaPoolSlots:
    @pytest.mark.gpu_renderer
    def test_alpha_slot_distinct_and_reused(self, gl_context, uploader) -> None:
        pool = uploader._pool
        y = pool.acquire_y_texture(W, H, 1)
        u = pool.acquire_u_texture(W // 2, H, 1)
        a1 = pool.acquire_a_texture(W, H, 1)
        assert a1 is not None and a1 is not y and a1 is not u
        # 第二帧：AUD-39804 后容量未满时 'a' 走新建槽位（不抢占在轨纹理），
        # 但仍按 plane_type 隔离，不与 y/u/v 串用
        a2 = pool.acquire_a_texture(W, H, 2)
        assert a2 is not None and a2 is not a1
        assert a2 is not y and a2 is not u
        assert a2 is not pool.acquire_y_texture(W, H, 2)
        # 填满 uv 池后，下一次 'a' 获取按 LRU 抢占最旧 'a' 槽（仍是 'a' 纹理）
        max_uv = pool.get_stats()["uv_max_size"]
        while len(pool._uv_slots) < max_uv:
            pool.acquire_a_texture(W, H, 3)
        a_textures = [s.texture for s in pool._uv_slots if s.plane_type == "a"]
        a_next = pool.acquire_a_texture(W, H, 4)
        assert any(t is a_next for t in a_textures)
        # 统计与释放
        stats = pool.get_stats()
        assert any(s[0] == W and s[1] == H for s in stats["uv_sizes"])
        pool.release_all()
        assert pool.get_stats()["uv_pool_size"] == 0

    @pytest.mark.gpu_renderer
    def test_release_clears_all_pools_and_reusable(self, gl_context) -> None:
        up = YUVGPUUploader(gl_context, pool_size=2)
        up.get_yuv_shader()  # 编译 shader
        texes = up.upload_yuv422(*_neutral_gray_planes()[:3], 1, 10)
        assert len(texes) == 3
        up.release()
        # 同一 context 上可再建再释放（无 GL 对象泄漏导致的崩溃）
        up2 = YUVGPUUploader(gl_context, pool_size=2)
        texes = up2.upload_yuv422(*_neutral_gray_planes()[:3], 1, 10)
        assert len(texes) == 3
        up2.release()


# =============================================================================
# GPU alpha 采样（float16 / R16UI 两路径）
# =============================================================================


class TestGPUAlphaSampling:
    @pytest.mark.gpu_renderer
    def test_float16_alpha_ramp_and_no_alpha_regression(self, gl_context, uploader) -> None:
        _disable_r16ui(uploader)
        y, c, _, a10 = _neutral_gray_planes()
        uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
        uploader.set_color_range(YUVGPUUploader.RANGE_FULL)

        # 带 alpha：四元组 + 读回 alpha == ramp/1023
        texes = uploader.upload_yuv422(y, c, c, 1, 10, a=a10, alpha_bit_depth=10)
        assert len(texes) == 4
        rgba = _render_and_read(
            gl_context, uploader, texes,
            bit_depth=10, alpha_bit_depth=10)
        expect = (a10.astype(np.float32) / 1023.0).astype(np.float16)
        assert np.max(np.abs(rgba[..., 3].astype(np.float32)
                             - expect.astype(np.float32))) < 2e-3
        # 中性灰 full range：rgb 三通道一致且 ≈ 512/1023
        assert np.all(np.abs(
            rgba[..., 0].astype(np.float32) - rgba[..., 1].astype(np.float32)) < 2e-3)
        assert abs(float(rgba[0, 0, 0]) - 512.0 / 1023.0) < 3e-3

        # 无 alpha：三元组 + alpha 恒 1.0（回归）
        texes3 = uploader.upload_yuv422(y, c, c, 2, 10)
        assert len(texes3) == 3
        rgba3 = _render_and_read(
            gl_context, uploader, texes3,
            bit_depth=10, alpha_bit_depth=10)
        assert np.all(rgba3[..., 3] == np.float16(1.0))

    @pytest.mark.gpu_renderer
    def test_r16ui_alpha_ramp_and_independent_bit_depth(self, gl_context, uploader) -> None:
        if not _r16ui_ok(uploader):
            pytest.skip("R16UI self-test failed on this GL context")
        y, c, _, a10 = _neutral_gray_planes()
        uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
        uploader.set_color_range(YUVGPUUploader.RANGE_FULL)

        # a10：alpha == ramp/1023
        texes = uploader.upload_yuv422_r16ui(y, c, c, 1, 10, a=a10, alpha_bit_depth=10)
        assert len(texes) == 4
        rgba = _render_and_read(
            gl_context, uploader, texes,
            bit_depth=10, alpha_bit_depth=10, force_r16ui=True)
        expect = a10.astype(np.float32) / 1023.0
        assert np.max(np.abs(rgba[..., 3].astype(np.float32) - expect)) < 2e-3

        # a8 独立位深：码值 0..255 存 u2，必须按 /255 归一化（若错用
        # luma 的 10-bit 会整体压暗 ~4×——本用例的关键断言）
        a8 = (np.arange(W)[None, :] * 255 // (W - 1)).astype("<u2")
        a8 = np.repeat(a8, H, axis=0)
        texes = uploader.upload_yuv422_r16ui(y, c, c, 2, 10, a=a8, alpha_bit_depth=8)
        assert len(texes) == 4
        rgba = _render_and_read(
            gl_context, uploader, texes,
            bit_depth=10, alpha_bit_depth=8, force_r16ui=True)
        expect = a8.astype(np.float32) / 255.0
        assert np.max(np.abs(rgba[..., 3].astype(np.float32) - expect)) < 2e-3
        # 末列 alpha ≈ 1.0（若误按 /1023 归一化，这里只有 ≈0.25）
        assert float(rgba[0, -1, 3]) > 0.9

    @pytest.mark.gpu_renderer
    def test_r8_alpha_path_8bit_yuva(self, gl_context, uploader) -> None:
        # 8-bit YUVA（FFmpeg yuva420p 布局）：uint8 直传 R8，sampler 自动 /255
        y = np.full((H, W), 128, np.uint8)
        c = np.full((H // 2, W // 2), 128, np.uint8)
        a = (np.arange(W)[None, :] * 255 // (W - 1)).astype(np.uint8)
        a = np.repeat(a, H, axis=0)
        uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
        uploader.set_color_range(YUVGPUUploader.RANGE_FULL)
        texes = uploader.upload_yuv420(y, c, c, 1, 8, a=a, alpha_bit_depth=8)
        assert len(texes) == 4

        out = gl_context.texture((W, H), components=4, dtype="f2")
        fbo = gl_context.framebuffer(color_attachments=[out])
        fbo.use()
        gl_context.viewport = (0, 0, W, H)
        gl_context.clear(0.0, 0.0, 0.0, 0.0)
        uploader.render_yuv_to_rgb(
            texes[0], texes[1], texes[2], fbo,
            bit_depth=8, a_tex=texes[3], alpha_bit_depth=8)
        rgba = np.frombuffer(
            fbo.read(viewport=(0, 0, W, H), components=4, dtype="f2"),
            dtype=np.float16,
        ).reshape(H, W, 4)
        expect = a.astype(np.float32) / 255.0
        assert np.max(np.abs(rgba[..., 3].astype(np.float32) - expect)) < 2e-3
        fbo.release()
        out.release()

    @pytest.mark.gpu_renderer
    def test_premultiplied_unpremultiply(self, gl_context, uploader) -> None:
        if not _r16ui_ok(uploader):
            pytest.skip("R16UI self-test failed on this GL context")
        y, c, _, a10 = _neutral_gray_planes()
        uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
        uploader.set_color_range(YUVGPUUploader.RANGE_FULL)
        texes = uploader.upload_yuv422_r16ui(y, c, c, 1, 10, a=a10, alpha_bit_depth=10)

        # straight（默认）：rgb 保持中性灰 ≈ 0.5
        rgba = _render_and_read(
            gl_context, uploader, texes,
            bit_depth=10, alpha_bit_depth=10, force_r16ui=True)
        mid = float(rgba[H // 2, W // 2, 0])
        assert abs(mid - 512.0 / 1023.0) < 5e-3

        # premultiplied：同码值下 rgb/alpha → 全透明区 rgb≈0、半透明区 ≈ 2×灰
        rgba_p = _render_and_read(
            gl_context, uploader, texes,
            bit_depth=10, alpha_bit_depth=10, force_r16ui=True,
            alpha_premultiplied=True)
        # alpha≈0.5 列：unpremult 后 rgb ≈ 0.5/0.5 = 1.0
        col = W // 2
        a_mid = float(a10[H // 2, col]) / 1023.0
        assert abs(a_mid - 0.5) < 0.05
        assert float(rgba_p[H // 2, col, 0]) > 0.95
        # alpha≈0 列：rgb 输出 0（除零保护），alpha 仍为 0
        assert float(rgba_p[H // 2, 0, 0]) == 0.0
        assert float(rgba_p[H // 2, 0, 3]) == 0.0

    @pytest.mark.gpu_renderer
    def test_hard_alpha_edge_no_fringe(self, gl_context, uploader) -> None:
        """硬 Alpha 边缘：straight 合成结果只含前景/背景值（无黑边/白边）。"""
        _disable_r16ui(uploader)
        y = np.full((H, W), 512, "<u2")          # 前景 50% 灰
        c = np.full((H, (W + 1) // 2), 512, "<u2")
        a = np.zeros((H, W), "<u2")
        a[:, W // 2:] = 1023                      # 左透明、右不透明
        uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
        uploader.set_color_range(YUVGPUUploader.RANGE_FULL)
        texes = uploader.upload_yuv422(y, c, c, 1, 10, a=a, alpha_bit_depth=10)
        rgba = _render_and_read(
            gl_context, uploader, texes,
            bit_depth=10, alpha_bit_depth=10)

        # GPU 读回：alpha 必须是二值（NEAREST 1:1 无滤波拖尾）
        assert np.all(np.logical_or(rgba[..., 3] == 0, rgba[..., 3] == np.float16(1.0)))
        # straight 合成到 25% 灰背景：每像素结果严格 ∈ {0.25, 前景灰}
        fg = 512.0 / 1023.0
        bg = 0.25
        comp = bg * (1.0 - rgba[..., 3].astype(np.float32)) + fg * rgba[..., 3].astype(np.float32)
        left = comp[:, : W // 2]
        right = comp[:, W // 2:]
        assert np.all(np.abs(left - bg) < 2e-3)
        assert np.all(np.abs(right - fg) < 5e-3)


# =============================================================================
# Topos decode → GPU 读回一致性（round-trip 门禁核心）
# =============================================================================


class TestToposDecodeToGPU:
    @pytest.mark.gpu_renderer
    def test_decode_gpu_alpha_matches_decoded_plane(
        self, gl_context, uploader, codec, tmp_path,
    ) -> None:
        from src.shared.media.topos_source import ToposMediaSource

        path = str(tmp_path / "alpha10.mov")
        _alpha_movie(codec, path, alpha_bit_depth=10)

        src = ToposMediaSource(path)
        src.open()
        try:
            frame = src.read_frame(2)
            assert len(frame.planes) == 4
            a_plane = frame.planes[3].astype(np.float32)
            a_max = float(frame.plane_infos[3].bit_depth)
            assert a_max == 10
            assert frame.extra.get("topos_alpha_premultiplied") is False

            uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
            uploader.set_color_range(YUVGPUUploader.RANGE_FULL)
            y, u, v, a = frame.planes
            if _r16ui_ok(uploader):
                texes = uploader.upload_yuv422_r16ui(
                    y, u, v, 1, 10, a=a, alpha_bit_depth=10)
                force = True
            else:
                _disable_r16ui(uploader)
                texes = uploader.upload_yuv422(
                    y, u, v, 1, 10, a=a, alpha_bit_depth=10)
                force = False
            assert len(texes) == 4
            rgba = _render_and_read(
                gl_context, uploader, texes,
                bit_depth=10, alpha_bit_depth=10, force_r16ui=force)

            # GPU 读回 alpha == 解码 alpha 平面（1:1 NEAREST 精确；
            # f16 量化 + 解码码值满分刻度 → 2e-3 容差）
            expect = a_plane / 1023.0
            err = np.max(np.abs(rgba[..., 3].astype(np.float32) - expect))
            assert err < 2e-3, f"GPU alpha mismatch: max err={err}"
        finally:
            src.close()

    @pytest.mark.gpu_renderer
    def test_decode_gpu_alpha8_independent_depth(
        self, gl_context, uploader, codec, tmp_path,
    ) -> None:
        from src.shared.media.topos_source import ToposMediaSource

        path = str(tmp_path / "alpha8.mov")
        _alpha_movie(codec, path, alpha_bit_depth=8)

        src = ToposMediaSource(path)
        src.open()
        try:
            frame = src.read_frame(0)
            assert len(frame.planes) == 4
            assert frame.plane_infos[3].bit_depth == 8
            assert frame.pixel_format == "topos_yuva422p10a8"

            uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
            uploader.set_color_range(YUVGPUUploader.RANGE_FULL)
            y, u, v, a = frame.planes
            _disable_r16ui(uploader)
            texes = uploader.upload_yuv422(
                y, u, v, 1, 10, a=a, alpha_bit_depth=8)
            assert len(texes) == 4
            rgba = _render_and_read(
                gl_context, uploader, texes,
                bit_depth=10, alpha_bit_depth=8)
            # 解码侧 a8 码值 0..255（u2 存储）；GPU 按 /255 归一化
            expect = a.astype(np.float32) / 255.0
            err = np.max(np.abs(rgba[..., 3].astype(np.float32) - expect))
            assert err < 2e-3, f"a8 alpha mismatch: max err={err}"
        finally:
            src.close()


# =============================================================================
# CPU fallback（duck-typed AsyncRenderer，无需 GL）
# =============================================================================


class _FakeUploadSink:
    def __init__(self) -> None:
        self.payload = None

    def _do_upload_video_frame(self, data):
        self.payload = data
        return object()


def _make_yuva_frame(alpha_bit_depth: int) -> "object":
    from src.shared.media.frame import DecodedFrame, PlaneInfo

    cw = (W + 1) // 2
    y = np.full((H, W), 512, "<u2")
    c = np.full((H, cw), 512, "<u2")
    a_max = (1 << alpha_bit_depth) - 1
    a = (np.arange(W)[None, :] * a_max // max(1, W - 1)).astype("<u2")
    a = np.repeat(a, H, axis=0)
    infos = (
        PlaneInfo.create(0, W, H, "Y", np.uint16, 10),
        PlaneInfo.create(1, cw, H, "U", np.uint16, 10),
        PlaneInfo.create(2, cw, H, "V", np.uint16, 10),
        PlaneInfo.create(3, W, H, "A", np.uint16, alpha_bit_depth),
    )
    pf = f"topos_yuva422p10a{alpha_bit_depth}"
    return DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0,
        planes=(y, c, c, a), pixel_format=pf,
        width=W, height=H, plane_infos=infos,
        extra={"topos_alpha_premultiplied": False},
    )


class TestCPUFallback:
    def test_alpha_frame_uploads_f2_with_correct_depth(self) -> None:
        from src.features.color.core.async_renderer import AsyncRenderer

        sink = _FakeUploadSink()
        frame = _make_yuva_frame(16)
        ret = AsyncRenderer._do_upload_planar_video_frame_cpu_fallback(
            sink, frame, 0, 0, 1)
        assert ret is not None
        assert sink.payload["dtype"] == "f2"
        rgba = np.frombuffer(
            sink.payload["frame_data"].tobytes(), dtype=np.float16,
        ).reshape(H, W, 4)
        # a16：alpha == ramp/65535（旧代码按 /1023 归一化 + 8-bit 量化）
        expect = frame.planes[3].astype(np.float32) / 65535.0
        err = np.max(np.abs(rgba[..., 3].astype(np.float32) - expect))
        assert err < 2e-3, f"a16 alpha err={err}"
        # 中性灰保留（不再被 clip 到 8-bit）
        assert abs(float(rgba[0, 0, 0]) - 512.0 / 1023.0) < 3e-3

    def test_alpha8_uses_alpha_plane_depth_not_luma(self) -> None:
        from src.features.color.core.async_renderer import AsyncRenderer

        sink = _FakeUploadSink()
        frame = _make_yuva_frame(8)
        ret = AsyncRenderer._do_upload_planar_video_frame_cpu_fallback(
            sink, frame, 0, 0, 1)
        assert ret is not None
        rgba = np.frombuffer(
            sink.payload["frame_data"].tobytes(), dtype=np.float16,
        ).reshape(H, W, 4)
        expect = frame.planes[3].astype(np.float32) / 255.0
        err = np.max(np.abs(rgba[..., 3].astype(np.float32) - expect))
        assert err < 2e-3, f"a8 alpha err={err}"
        assert float(rgba[0, -1, 3]) > 0.99  # 误用 /1023 时这里只有 ~0.25

    def test_no_alpha_frame_stays_f1_opaque(self) -> None:
        from src.features.color.core.async_renderer import AsyncRenderer
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        cw = (W + 1) // 2
        y = np.full((H, W), 512, "<u2")
        c = np.full((H, cw), 512, "<u2")
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(y, c, c), pixel_format="yuv422p10le",
            width=W, height=H,
            plane_infos=(
                PlaneInfo.create(0, W, H, "Y", np.uint16, 10),
                PlaneInfo.create(1, cw, H, "U", np.uint16, 10),
                PlaneInfo.create(2, cw, H, "V", np.uint16, 10),
            ),
        )
        sink = _FakeUploadSink()
        ret = AsyncRenderer._do_upload_planar_video_frame_cpu_fallback(
            sink, frame, 0, 0, 1)
        assert ret is not None
        # 无 Alpha：维持 f1（内存画像不变）且 alpha=255
        assert sink.payload["dtype"] == "f1"
        rgba = np.frombuffer(
            sink.payload["frame_data"].tobytes(), dtype=np.uint8,
        ).reshape(H, W, 4)
        assert np.all(rgba[..., 3] == 255)
