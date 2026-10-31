"""Regression tests for batch A critical fixes (w5-g2, shared/video).

Covers:
  - AUD-39500: 预分配转换缓冲跨线程别名 —— upload_and_render_frame 排入
    队列的帧必须是独立缓冲，生产者复写暂存池不得改写队列中待消费帧。
  - AUD-39501: YUV 上传路径必须把合成 RGB 帧写入 current_frame_tex，
    RENDER_FRAME 渲染的不再是过期 RGB 帧。
  - AUD-39600: 高位深 BGR packed（bgra64 等 uint16）转 uint8 必须按
    /65535*255 缩放，旧实现 /(1<<bit_depth) 截断后全黑。
  - AUD-39800: BT.601 数字 YCbCr 矩阵系数（R:+1.402 / G:-0.3441U-0.7141V
    / B:+1.772），四处 shader 副本同改，PAL 模拟系数不得回归。

All tests are headless / GL-free: renderer internals are driven on bare
instances with fake context/uploader objects, so no QApplication or
OpenGL context is required.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.video.video_async_renderer import VideoAsyncRenderer, VideoRenderCommand
from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
from src.shared.video.yuv_gpu_upload import YUVGPUUploader


# ---------------------------------------------------------------------------
# Helpers: bare VideoAsyncRenderer (no Qt/GL), fake ctx / uploader
# ---------------------------------------------------------------------------


def _make_bare_renderer():
    """构造未启动线程、未创建 GL 资源的 VideoAsyncRenderer。

    upload_and_render_frame 只依赖暂存缓冲状态与 submit()；
    submit 被替换为采集器以截获排入命令队列的 payload。
    """
    renderer = VideoAsyncRenderer.__new__(VideoAsyncRenderer)
    renderer._convert_buffer = None
    renderer._buffer_size = (0, 0)
    renderer.current_frame_index = -1
    renderer.current_frame_tex = None
    renderer._yuv_composite_fbo = None
    renderer._yuv_composite_tex = None
    # _cleanup_gl_resources 遍历的其余状态（bare 实例上无 __init__）
    renderer._initialized = False
    renderer._video_texture_pool = None
    renderer._yuv_gpu_uploader = None
    renderer._yuv_y_tex = None
    renderer._yuv_u_tex = None
    renderer._yuv_v_tex = None
    renderer._yuv_uv_tex = None
    renderer._is_yuv_frame = False
    renderer._yuv_format = None
    renderer.fbo_pair = [None, None]
    renderer.tex_pair = [None, None]
    renderer.ctx = None
    # wd17-g102 引入的 fallback 纹理清理状态（缺失时 PyQt6 __new__ 桩上
    # 的属性访问抛 RuntimeError，曾致 test_cleanup_releases_composite_
    # resources 预存失败）
    renderer._fallback_frame_tex = None
    renderer._fallback_frame_tex_desc = None

    captured: list = []

    def _capture(cmd_type, data=None):
        captured.append((cmd_type, data))

    renderer.submit = _capture
    return renderer, captured


class _FakeTexture:
    def __init__(self, size):
        self.size = size
        self.released = False

    def release(self):
        self.released = True


class _FakeFramebuffer:
    def __init__(self, tex):
        self.size = tex.size
        self.color_attachments = [tex]
        self.released = False

    def release(self):
        self.released = True


class _FakeCtx:
    def __init__(self):
        self.textures: list = []

    def texture(self, size, components, dtype="f2"):
        tex = _FakeTexture(size)
        self.textures.append(tex)
        return tex

    def framebuffer(self, tex):
        return _FakeFramebuffer(tex)


class _FakeUploader:
    """仅实现 _do_upload_yuv_frame 消费的接口。"""

    MATRIX_BT709 = YUVGPUUploader.MATRIX_BT709
    MATRIX_BT2020 = YUVGPUUploader.MATRIX_BT2020
    MATRIX_BT601 = YUVGPUUploader.MATRIX_BT601
    RANGE_LIMITED = YUVGPUUploader.RANGE_LIMITED

    def __init__(self):
        self.yuv_matrix = None
        self.color_range = None
        self.planar_renders: list = []
        self.nv12_renders: list = []
        self._y = _FakeTexture((8, 4))
        self._u = _FakeTexture((4, 2))
        self._v = _FakeTexture((4, 2))
        self._uv = _FakeTexture((4, 2))

    def set_yuv_matrix(self, matrix):
        self.yuv_matrix = matrix

    def set_color_range(self, range_type):
        self.color_range = range_type

    def upload_yuv420(self, y, u, v, frame_index, bit_depth=8):
        return self._y, self._u, self._v

    def upload_yuv422(self, y, u, v, frame_index, bit_depth=8):
        return self._y, self._u, self._v

    def upload_yuv444(self, y, u, v, frame_index, bit_depth=8):
        return self._y, self._u, self._v

    def upload_nv12(self, y, uv, frame_index, bit_depth=8):
        return self._y, self._uv

    def upload_p010(self, y, uv, frame_index, bit_depth=10):
        return self._y, self._uv

    def render_yuv_to_rgb(self, y_tex, u_tex, v_tex, output_fbo, **kwargs):
        self.planar_renders.append((y_tex, u_tex, v_tex, output_fbo, kwargs))

    def render_nv12_to_rgb(self, y_tex, uv_tex, output_fbo, **kwargs):
        self.nv12_renders.append((y_tex, uv_tex, output_fbo, kwargs))

    def get_upload_stats(self):
        return SimpleNamespace(avg_upload_time_ms=0.5)

    def calculate_bandwidth_saved(self, width, height, yuv_format):
        return 1.0

    def release(self):
        pass


def _make_yuv_renderer():
    renderer, _ = _make_bare_renderer()
    renderer.ctx = _FakeCtx()
    renderer._yuv_gpu_uploader = _FakeUploader()
    return renderer, renderer._yuv_gpu_uploader


def _yuv420_payload(frame_index=0):
    return {
        "y": np.zeros((4, 8), dtype=np.uint8),
        "u": np.full((2, 4), 128, dtype=np.uint8),
        "v": np.full((2, 4), 128, dtype=np.uint8),
        "format": "420",
        "bit_depth": 8,
        "frame_index": frame_index,
        "yuv_matrix": YUVGPUUploader.MATRIX_BT601,
        "color_range": YUVGPUUploader.RANGE_LIMITED,
    }


# ---------------------------------------------------------------------------
# AUD-39500: 预分配转换缓冲跨线程别名
# ---------------------------------------------------------------------------


class TestConvertBufferNoAliasAcrossThreads:
    def test_queued_frames_are_independent_buffers(self):
        """连续两帧入队后，队列中两条 UPLOAD_FRAME 的缓冲必须相互独立，
        且第一条的内容不被第二帧转换复写。"""
        renderer, captured = _make_bare_renderer()

        frame1 = np.zeros((4, 8, 3), dtype=np.uint8)
        frame1[:, :] = (10, 20, 30)  # BGR
        frame2 = np.full((4, 8, 3), 200, dtype=np.uint8)

        renderer.upload_and_render_frame(frame1, (8, 4))
        renderer.upload_and_render_frame(frame2, (8, 4))

        uploads = [d for c, d in captured if c == VideoRenderCommand.UPLOAD_FRAME]
        assert len(uploads) == 2

        img1 = uploads[0]["image"]
        img2 = uploads[1]["image"]

        # 独立缓冲：两条队列项不共享同一 numpy 对象
        assert img1 is not img2, "AUD-39500: 队列中待消费帧不得共享暂存缓冲"

        # 第一帧内容在第二帧转换后保持不变（旧实现此处已被复写为 frame2）
        expected_rgb = np.empty((4, 8, 3), dtype=np.float32)
        expected_rgb[..., 0] = 30 / 255.0
        expected_rgb[..., 1] = 20 / 255.0
        expected_rgb[..., 2] = 10 / 255.0
        np.testing.assert_allclose(img1, expected_rgb, atol=1e-6)

        # 转换语义保持：float32、[0,1]
        assert img2.dtype == np.float32
        assert img2.min() >= 0.0 and img2.max() <= 1.0

    def test_scratch_buffer_still_reused_within_producer_thread(self):
        """暂存池仍在调用方线程内复用（不退化为每帧两次全量分配之外的
        额外常驻拷贝），但队列中的帧不再是暂存缓冲本身。"""
        renderer, captured = _make_bare_renderer()

        frame = np.zeros((4, 8, 3), dtype=np.uint8)
        renderer.upload_and_render_frame(frame, (8, 4))
        scratch_after_first = renderer._convert_buffer

        renderer.upload_and_render_frame(frame, (8, 4))

        assert renderer._convert_buffer is scratch_after_first, (
            "暂存缓冲应在线程内复用"
        )
        uploads = [d for c, d in captured if c == VideoRenderCommand.UPLOAD_FRAME]
        assert uploads[0]["image"] is not renderer._convert_buffer
        assert uploads[1]["image"] is not renderer._convert_buffer

    def test_uint16_input_also_decoupled_from_scratch(self):
        """uint16 高位深路径（_convert_frame_to_float_generic 非uint8 分支）
        同样不得把暂存缓冲排入队列。"""
        renderer, captured = _make_bare_renderer()

        frame1 = np.full((4, 8, 3), 60000, dtype=np.uint16)
        frame2 = np.zeros((4, 8, 3), dtype=np.uint16)

        renderer.upload_and_render_frame(frame1, (8, 4))
        renderer.upload_and_render_frame(frame2, (8, 4))

        uploads = [d for c, d in captured if c == VideoRenderCommand.UPLOAD_FRAME]
        img1 = uploads[0]["image"]
        assert img1 is not uploads[1]["image"]
        np.testing.assert_allclose(
            img1, np.full((4, 8, 3), 60000 / 65535.0, dtype=np.float32), atol=1e-6
        )


# ---------------------------------------------------------------------------
# AUD-39501: YUV 渲染路径必须设置 current_frame_tex
# ---------------------------------------------------------------------------


class TestYuvUploadSetsCurrentFrameTex:
    def test_planar_yuv_upload_composites_and_sets_current_tex(self):
        renderer, uploader = _make_yuv_renderer()

        assert renderer.current_frame_tex is None

        renderer._do_upload_yuv_frame(_yuv420_payload(frame_index=7))

        # 合成输出纹理写入 current_frame_tex（对齐 RGB 路径）
        assert renderer.current_frame_tex is not None, (
            "AUD-39501: YUV 上传后 current_frame_tex 仍为 None，"
            "RENDER_FRAME 将静默不渲染或渲染过期 RGB 帧"
        )
        assert renderer.current_frame_tex is renderer._yuv_composite_tex

        # 合成 FBO 为帧尺寸，且 render_yuv_to_rgb 收到该 FBO
        assert renderer._yuv_composite_fbo.size == (8, 4)
        assert len(uploader.planar_renders) == 1
        y_tex, u_tex, v_tex, fbo, kwargs = uploader.planar_renders[0]
        assert fbo is renderer._yuv_composite_fbo
        assert y_tex is uploader._y
        assert kwargs.get("bit_depth") == 8

        # 帧状态同步更新
        assert renderer.current_frame_index == 7
        assert renderer.frame_size == (8, 4)

    def test_nv12_yuv_upload_sets_current_tex_via_nv12_render(self):
        renderer, uploader = _make_yuv_renderer()

        payload = {
            "y": np.zeros((4, 8), dtype=np.uint8),
            "uv": np.zeros((2, 4, 2), dtype=np.uint8),
            "format": "nv12",
            "bit_depth": 8,
            "frame_index": 3,
            "yuv_matrix": YUVGPUUploader.MATRIX_BT709,
            "color_range": YUVGPUUploader.RANGE_LIMITED,
        }
        renderer._do_upload_yuv_frame(payload)

        assert renderer.current_frame_tex is renderer._yuv_composite_tex
        assert len(uploader.nv12_renders) == 1
        assert uploader.nv12_renders[0][2] is renderer._yuv_composite_fbo
        assert uploader.planar_renders == []

    def test_composite_fbo_recreated_on_size_change(self):
        renderer, uploader = _make_yuv_renderer()

        renderer._do_upload_yuv_frame(_yuv420_payload(frame_index=0))
        first_fbo = renderer._yuv_composite_fbo
        first_tex = renderer._yuv_composite_tex

        big = _yuv420_payload(frame_index=1)
        big["y"] = np.zeros((16, 32), dtype=np.uint8)
        big["u"] = np.full((8, 16), 128, dtype=np.uint8)
        big["v"] = np.full((8, 16), 128, dtype=np.uint8)
        renderer._do_upload_yuv_frame(big)

        assert renderer._yuv_composite_fbo is not first_fbo
        assert renderer._yuv_composite_fbo.size == (32, 16)
        assert renderer.current_frame_tex is renderer._yuv_composite_tex
        # 旧资源被释放（FBO 与颜色附件纹理分别释放）
        assert first_fbo.released
        assert first_tex.released

    def test_cleanup_releases_composite_resources(self):
        renderer, uploader = _make_yuv_renderer()
        renderer._do_upload_yuv_frame(_yuv420_payload(frame_index=0))

        fbo = renderer._yuv_composite_fbo
        tex = renderer._yuv_composite_tex

        renderer._cleanup_gl_resources()

        assert renderer._yuv_composite_fbo is None
        assert renderer._yuv_composite_tex is None
        assert renderer.current_frame_tex is None
        assert fbo.released
        assert tex.released


# ---------------------------------------------------------------------------
# AUD-39600: 高位深 BGR packed 转 uint8 缩放
# ---------------------------------------------------------------------------


class TestBgr16PackedToUint8Scaling:
    def _process(self, data, pixel_format, frame_extra=None):
        frame = SimpleNamespace(data=data, pixel_format=pixel_format)
        for k, v in (frame_extra or {}).items():
            setattr(frame, k, v)
        return EnhancedVideoPlaybackEngine._process_decoded_frame(
            SimpleNamespace(), frame
        )

    def test_bgra64_uint16_scales_to_visible_range(self):
        """16-bit BGR 帧不得输出全黑：65535→255、32768→127、0→0。"""
        data = np.array([[[65535, 32768, 0, 65535]]], dtype=np.uint16)
        result = self._process(data, "bgra64", {"bit_depth": 16})

        assert result is not None
        assert result.dtype == np.uint8
        assert result.shape == (1, 1, 3)
        assert not np.all(result == 0), (
            "AUD-39600: 16-bit BGR 帧输出全黑（旧实现 /(1<<bit_depth) 截断为 0）"
        )
        assert int(result[0, 0, 0]) == 255
        assert int(result[0, 0, 1]) == 127  # 32768/65535*255 = 127.996 截断
        assert int(result[0, 0, 2]) == 0

    def test_bgra64_without_bit_depth_attribute(self):
        """bit_depth 属性缺失时不再抛 AttributeError（异常会被外层捕获并
        返回 None，导致首帧/HQ seek 黑屏）。"""
        data = np.full((1, 1, 4), 65535, dtype=np.uint16)
        result = self._process(data, "bgra64")

        assert result is not None
        assert int(result[0, 0, 0]) == 255

    def test_bgr24_uint8_path_unchanged(self):
        data = np.array([[[10, 20, 30]]], dtype=np.uint8)
        result = self._process(data, "bgr24")
        assert result[0, 0].tolist() == [10, 20, 30]

    def test_matches_process_frame_convention(self):
        """与 _process_frame（预读 worker）同一转换惯例：同输入同输出。"""
        data = np.array([[[65535, 32768, 0, 65535]]], dtype=np.uint16)
        result = self._process(data, "bgra64", {"bit_depth": 16})

        expected = (data[..., :3] / 65535.0 * 255.0).astype(np.uint8)
        np.testing.assert_array_equal(result, expected)


# ---------------------------------------------------------------------------
# AUD-39800: BT.601 数字 YCbCr 矩阵系数
# ---------------------------------------------------------------------------


class TestBt601DigitalMatrixCoefficients:
    def test_standard_601_coefficients_in_all_shader_copies(self):
        """四处 shader 副本（float 3-plane / r16ui 3-plane / nv12 r16ui /
        nv12 float）全部使用标准数字 601 系数。"""
        source = inspect.getsource(YUVGPUUploader)

        assert source.count("0.0, -0.3441, 1.7720,") == 4
        assert source.count("1.4020, -0.7141, 0.0") == 4

    def test_pal_analog_coefficients_removed(self):
        source = inspect.getsource(YUVGPUUploader)

        for pal_coeff in ("-0.3945", "2.032", "1.140", "-0.581"):
            assert pal_coeff not in source, (
                f"AUD-39800: PAL 模拟系数 {pal_coeff} 仍存在于 shader 源"
            )

    def test_coefficients_match_bt601_derivation(self):
        """从 Kr=0.299/Kb=0.114 推导标准 601 数字 YCbCr 系数，并核对
        shader 常量（列布局与已核对无误的 BT.709 行一致）。"""
        kr, kb = 0.299, 0.114
        kg = 1.0 - kr - kb

        r_from_v = 2.0 * (1.0 - kr)          # 1.402
        b_from_u = 2.0 * (1.0 - kb)          # 1.772
        g_from_u = 2.0 * kb * (1.0 - kb) / kg  # 0.3441
        g_from_v = 2.0 * kr * (1.0 - kr) / kg  # 0.7141

        assert r_from_v == pytest.approx(1.4020, abs=1e-4)
        assert b_from_u == pytest.approx(1.7720, abs=1e-4)
        assert g_from_u == pytest.approx(0.3441, abs=1e-4)
        assert g_from_v == pytest.approx(0.7141, abs=1e-4)

        # 同一推导应用于 BT.709（shader 中已核对无误）验证列布局惯例一致
        kr9, kb9 = 0.2126, 0.0722
        kg9 = 1.0 - kr9 - kb9
        assert 2.0 * (1.0 - kr9) == pytest.approx(1.5748, abs=1e-4)
        assert 2.0 * kb9 * (1.0 - kb9) / kg9 == pytest.approx(0.1873, abs=1e-4)
        assert 2.0 * kr9 * (1.0 - kr9) / kg9 == pytest.approx(0.4681, abs=1e-4)

    def test_bt601_matrix_code_reachable_from_sd_tags(self):
        """SD 内容（smpte170m/bt470bg）映射到 MATRIX_BT601，601 路径真实可达。"""
        assert YUVGPUUploader.MATRIX_BT601 == 2

        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine as _Eng

        for tag in ("smpte170m", "bt470bg"):
            frame = SimpleNamespace(
                metadata=SimpleNamespace(color_matrix=tag)
            )
            assert _Eng._get_yuv_matrix(SimpleNamespace(), frame) == 2


# ---------------------------------------------------------------------------
# AUD-39800: GL 端像素级验证（无 GL 上下文环境自动跳过）
# ---------------------------------------------------------------------------


@pytest.fixture
def gl_context():
    try:
        import moderngl

        ctx = moderngl.create_standalone_context()
        yield ctx
        ctx.release()
    except Exception as e:  # pragma: no cover - 环境相关
        pytest.skip(f"OpenGL context unavailable: {e}")


class TestBt601GpuPixelVerification:
    def test_gpu_bt601_output_matches_standard_reference(self, gl_context):
        """渲染一帧 limited-range 601 测试色，GPU 输出必须与标准数字
        BT.601 CPU 参考一致（旧 PAL 系数误差可达 ~20/255）。"""
        h, w = 4, 8
        y = np.full((h, w), 128, dtype=np.uint8)
        u = np.full((h // 2, w // 2), 62, dtype=np.uint8)
        v = np.full((h // 2, w // 2), 194, dtype=np.uint8)

        uploader = YUVGPUUploader(gl_context)
        uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT601)
        uploader.set_color_range(YUVGPUUploader.RANGE_LIMITED)

        y_tex, u_tex, v_tex = uploader.upload_yuv420(y, u, v, frame_index=0)
        out_tex = gl_context.texture((w, h), 4, dtype="f1")
        fbo = gl_context.framebuffer(out_tex)
        uploader.render_yuv_to_rgb(y_tex, u_tex, v_tex, fbo, bit_depth=8)
        raw = np.frombuffer(fbo.read(components=3), dtype=np.uint8).reshape(h, w, 3)

        # CPU 参考：limited range 归一化 + 标准数字 601 矩阵
        yy = (128 - 16) * 255.0 / 219.0
        cb = (62 - 128) * 255.0 / 224.0
        cr = (194 - 128) * 255.0 / 224.0
        ref = np.clip(
            np.rint(
                np.array(
                    [
                        yy + 1.402 * cr,
                        yy - 0.3441 * cb - 0.7141 * cr,
                        yy + 1.772 * cb,
                    ]
                )
            ),
            0,
            255,
        ).astype(np.int32)

        gpu_px = raw[h // 2, w // 2].astype(np.int32)
        assert np.abs(gpu_px - ref).max() <= 1, (
            f"GPU {gpu_px.tolist()} != 标准 601 参考 {ref.tolist()}"
        )

        # 旧 PAL 系数会显著偏离（确认回归敏感度）
        pal = np.clip(
            np.rint(
                np.array(
                    [
                        yy + 1.140 * cr,
                        yy - 0.3945 * cb - 0.581 * cr,
                        yy + 2.032 * cb,
                    ]
                )
            ),
            0,
            255,
        ).astype(np.int32)
        assert np.abs(gpu_px - pal).max() > 10

        uploader.release()
        out_tex.release()
        fbo.release()
