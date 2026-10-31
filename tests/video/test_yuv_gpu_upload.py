"""
YUV GPU Upload Tests - YUV 平面直接 GPU 上传测试

测试覆盖范围：
1. YUVGPUUploader 基础功能
2. YUVTexturePool 纹理池管理
3. YUV420/422/444 格式上传
4. NV12/P010 格式上传
5. 高位深支持 (10-bit, 12-bit)
6. 带宽减少验证
7. Shader 创建和渲染

注意事项：
- OpenGL 测试需要创建 standalone context
- 所有 ModernGL 操作在测试线程执行
- 测试后必须清理资源
"""

import pytest
import sys
import os
import time
import logging
import numpy as np
from typing import Tuple, Dict, Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

logger = logging.getLogger(__name__)

# 检查 moderngl 是否可用
try:
    import moderngl
    HAS_MODERNGL = True
except ImportError:
    HAS_MODERNGL = False

# 导入模块
try:
    from src.shared.video.yuv_gpu_upload import (
        YUVGPUUploader,
        YUVTexturePool,
        YUVTextureSlot,
        YUVUploadStats,
        get_chroma_uv_params,
        get_chroma_scale,
        create_test_yuv_frame,
        create_test_nv12_frame,
    )
    HAS_YUV_MODULE = True
except ImportError:
    HAS_YUV_MODULE = False


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def gl_context():
    """创建 standalone ModernGL 上下文"""
    if not HAS_MODERNGL:
        pytest.skip("moderngl not available")

    try:
        ctx = moderngl.create_standalone_context()
        yield ctx
    except Exception as e:
        pytest.skip(f"Failed to create OpenGL context: {e}")


@pytest.fixture
def yuv_uploader(gl_context):
    """创建 YUVGPUUploader 实例"""
    if not HAS_YUV_MODULE:
        pytest.skip("YUVGPUUploader module not available")

    uploader = YUVGPUUploader(gl_context, pool_size=5)
    yield uploader
    uploader.release()


# =============================================================================
# YUVTextureSlot Tests
# =============================================================================

class TestYUVTextureSlot:
    """测试 YUVTextureSlot 数据结构"""

    def test_slot_creation(self) -> None:
        """槽位创建"""
        slot = YUVTextureSlot(
            width=1920,
            height=1080,
            frame_index=0,
            plane_type='y'
        )

        assert slot.width == 1920
        assert slot.height == 1080
        assert slot.frame_index == 0
        assert slot.plane_type == 'y'

    def test_size_match(self) -> None:
        """尺寸匹配检查"""
        slot = YUVTextureSlot(width=1920, height=1080)

        assert slot.is_size_match(1920, 1080) == True
        assert slot.is_size_match(1920, 1081) == False
        assert slot.is_size_match(3840, 2160) == False

    def test_availability(self) -> None:
        """可用性检查
        
        槽位只有当 frame_index < 0 时才被视为空闲。
        如果 texture 为 None 但 frame_index >= 0，说明状态不一致，
        不应视为空闲。
        """
        slot1 = YUVTextureSlot(frame_index=-1)
        assert slot1.is_available() == True

        slot2 = YUVTextureSlot(frame_index=10)
        assert slot2.is_available() == False

        # texture=None 但 frame_index=-1，视为空闲
        slot3 = YUVTextureSlot(texture=None, frame_index=-1)
        assert slot3.is_available() == True

        # texture=None 但 frame_index >= 0，状态不一致，不视为空闲
        slot4 = YUVTextureSlot(texture=None, frame_index=5)
        assert slot4.is_available() == False


# =============================================================================
# YUVTexturePool Tests
# =============================================================================

@pytest.mark.skipif(not HAS_MODERNGL, reason="moderngl not available")
class TestYUVTexturePool:
    """测试 YUV 纹理池"""

    def test_pool_creation(self, gl_context) -> None:
        """池创建"""
        pool = YUVTexturePool(gl_context, y_pool_size=5, uv_pool_size=5)

        stats = pool.get_stats()
        assert stats['y_pool_size'] == 0  # 初始无槽位
        assert stats['y_max_size'] == 5
        assert stats['uv_pool_size'] == 0
        assert stats['uv_max_size'] == 5

        pool.release_all()

    def test_acquire_y_texture(self, gl_context) -> None:
        """获取 Y 平面纹理"""
        pool = YUVTexturePool(gl_context)

        tex = pool.acquire_y_texture(1920, 1080, frame_index=0)

        assert tex is not None
        assert tex.size == (1920, 1080)
        assert tex.components == 1

        pool.release_all()

    def test_acquire_uv_texture(self, gl_context) -> None:
        """获取 UV 平面纹理"""
        pool = YUVTexturePool(gl_context)

        # NV12 interleaved UV (2 通道)
        tex = pool.acquire_uv_texture(960, 540, frame_index=0, components=2)

        assert tex is not None
        assert tex.size == (960, 540)
        assert tex.components == 2

        pool.release_all()

    def test_texture_reuse(self, gl_context) -> None:
        """纹理复用（AUD-39804：容量未满时新建槽位，池满后按 LRU 抢占最旧）"""
        pool = YUVTexturePool(gl_context, y_pool_size=2)

        # 第一次获取
        tex1 = pool.acquire_y_texture(1920, 1080, frame_index=0)
        tex1_id = tex1.glo

        # 第二次获取相同尺寸：容量未满 → 新建独立槽位，
        # 不抢占仍在轨的上一帧纹理（旧实现直接抢占，稳态仅 1 张纹理）
        tex2 = pool.acquire_y_texture(1920, 1080, frame_index=1)
        assert tex2.glo != tex1_id

        stats = pool.get_stats()
        assert stats['y_pool_size'] == 2  # 池已满

        # 池满后第三次获取：抢占最旧槽位（frame 0）→ 复用 tex1 的纹理
        tex3 = pool.acquire_y_texture(1920, 1080, frame_index=2)
        assert tex3.glo == tex1_id
        assert pool.get_stats()['y_pool_size'] == 2

        pool.release_all()

    def test_pool_size_limit(self, gl_context) -> None:
        """池大小限制"""
        pool = YUVTexturePool(gl_context, y_pool_size=2)

        # 创建多个不同尺寸纹理
        tex1 = pool.acquire_y_texture(1920, 1080, frame_index=0)
        tex2 = pool.acquire_y_texture(1280, 720, frame_index=1)
        tex3 = pool.acquire_y_texture(640, 480, frame_index=2)  # 应替换最旧的

        stats = pool.get_stats()
        assert stats['y_pool_size'] <= 2

        pool.release_all()

    def test_upload_stats(self, gl_context) -> None:
        """上传统计"""
        pool = YUVTexturePool(gl_context)

        pool.update_upload_stats(1000, 500, 10.0)
        pool.update_upload_stats(1000, 500, 12.0)

        stats = pool.get_upload_stats()
        assert stats.frame_count == 2
        assert stats.avg_upload_time_ms == 11.0
        assert stats.y_bytes == 2000
        assert stats.uv_bytes == 1000

        pool.release_all()


# =============================================================================
# YUVGPUUploader Tests
# =============================================================================

@pytest.mark.skipif(not HAS_MODERNGL or not HAS_YUV_MODULE, reason="moderngl or YUV module not available")
class TestYUVGPUUploader:
    """测试 YUV GPU 上传器"""

    def test_uploader_creation(self, yuv_uploader) -> None:
        """上传器创建"""
        stats = yuv_uploader.get_pool_stats()
        assert 'y_pool_size' in stats
        assert 'uv_pool_size' in stats

    def test_upload_yuv420_8bit(self, yuv_uploader) -> None:
        """上传 YUV420 8-bit"""
        y, u, v = create_test_yuv_frame(1920, 1080, '420', 8)

        y_tex, u_tex, v_tex = yuv_uploader.upload_yuv420(y, u, v, frame_index=0)

        assert y_tex is not None
        assert u_tex is not None
        assert v_tex is not None

        assert y_tex.size == (1920, 1080)
        assert u_tex.size == (960, 540)
        assert v_tex.size == (960, 540)

        stats = yuv_uploader.get_upload_stats()
        assert stats.frame_count == 1

    def test_upload_yuv420_10bit(self, yuv_uploader) -> None:
        """上传 YUV420 10-bit"""
        y, u, v = create_test_yuv_frame(1920, 1080, '420', 10)

        y_tex, u_tex, v_tex = yuv_uploader.upload_yuv420(y, u, v, frame_index=0, bit_depth=10)

        assert y_tex is not None
        assert y_tex.size == (1920, 1080)

    def test_upload_yuv422(self, yuv_uploader) -> None:
        """上传 YUV422"""
        y, u, v = create_test_yuv_frame(1920, 1080, '422', 8)

        y_tex, u_tex, v_tex = yuv_uploader.upload_yuv422(y, u, v, frame_index=0)

        assert y_tex.size == (1920, 1080)
        assert u_tex.size == (960, 1080)  # 422: 水平减半，垂直不变
        assert v_tex.size == (960, 1080)

    def test_upload_yuv444(self, yuv_uploader) -> None:
        """上传 YUV444"""
        y, u, v = create_test_yuv_frame(1920, 1080, '444', 8)

        y_tex, u_tex, v_tex = yuv_uploader.upload_yuv444(y, u, v, frame_index=0)

        assert y_tex.size == (1920, 1080)
        assert u_tex.size == (1920, 1080)  # 444: 所有平面相同尺寸
        assert v_tex.size == (1920, 1080)

    def test_upload_yuv_planar_12bit_neutral_gray_renders_correctly(
        self,
        yuv_uploader,
        gl_context,
    ) -> None:
        """12-bit planar uploads must not use the unsafe u2/sampler2D path.

        A neutral limited-range 12-bit YUV value should render as neutral gray.
        The previously attempted R16 fast path sampled integer textures with a
        float sampler and produced green/black output instead.
        """
        y = np.full((4, 4), 2048, dtype=np.uint16)
        u = np.full((4, 4), 2048, dtype=np.uint16)
        v = np.full((4, 4), 2048, dtype=np.uint16)

        # Without self-test passing, upload_yuv_planar should fall back to float16
        y_tex, u_tex, v_tex = yuv_uploader.upload_yuv_planar(
            y, u, v, frame_index=0, bit_depth=12, subsampling='444'
        )

        output_tex = gl_context.texture((4, 4), components=4, dtype='f1')
        output_fbo = gl_context.framebuffer(color_attachments=[output_tex])

        yuv_uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
        yuv_uploader.set_color_range(YUVGPUUploader.RANGE_LIMITED)
        yuv_uploader.render_yuv_to_rgb(
            y_tex, u_tex, v_tex, output_fbo, bit_depth=12
        )

        pixels = np.frombuffer(
            output_fbo.read(components=4), dtype=np.uint8
        ).reshape(4, 4, 4)
        r, g, b, _ = pixels[0, 0]

        assert 120 <= int(r) <= 140, f"R={r} not in [120, 140]"
        assert abs(int(r) - int(g)) <= 2, f"|R-G| = {abs(int(r)-int(g))} > 2"
        assert abs(int(r) - int(b)) <= 2, f"|R-B| = {abs(int(r)-int(b))} > 2"

        output_fbo.release()
        output_tex.release()

    def test_r16ui_12bit_neutral_gray_renders_correctly(
        self,
        yuv_uploader,
        gl_context,
    ) -> None:
        """R16UI/usampler2D path must render 12-bit neutral gray correctly.

        Runs the R16UI self-test and verifies neutral gray rendering.
        This validates that usampler2D + integer textures produce correct colors.
        """
        # Run self-test first — this sets up the R16UI shader and validates colors
        passed = yuv_uploader.run_r16ui_self_test()
        assert passed, (
            f"R16UI self-test failed: "
            f"{getattr(yuv_uploader, '_r16ui_disabled_reason', 'unknown')}"
        )

        # Upload and render neutral gray via R16UI
        y = np.full((4, 4), 2048, dtype=np.uint16)
        u = np.full((4, 4), 2048, dtype=np.uint16)
        v = np.full((4, 4), 2048, dtype=np.uint16)

        y_tex, u_tex, v_tex = yuv_uploader.upload_yuv444_r16ui(
            y, u, v, frame_index=0, bit_depth=12
        )

        output_tex = gl_context.texture((4, 4), components=4, dtype='f1')
        output_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        output_fbo = gl_context.framebuffer(color_attachments=[output_tex])

        yuv_uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
        yuv_uploader.set_color_range(YUVGPUUploader.RANGE_LIMITED)

        output_fbo.use()
        gl_context.viewport = (0, 0, 4, 4)
        gl_context.clear(0.0, 0.0, 0.0, 0.0)
        yuv_uploader.render_yuv_r16ui_to_rgb(
            y_tex, u_tex, v_tex, output_fbo, bit_depth=12
        )

        pixels = np.frombuffer(
            output_fbo.read(components=4), dtype=np.uint8
        ).reshape(4, 4, 4)
        r, g, b, _ = pixels[0, 0]

        # Neutral gray should be approximately (130, 130, 130)
        assert 120 <= int(r) <= 140, f"R={r} not in [120, 140]"
        assert abs(int(r) - int(g)) <= 2, f"|R-G| = {abs(int(r)-int(g))} > 2"
        assert abs(int(r) - int(b)) <= 2, f"|R-B| = {abs(int(r)-int(b))} > 2"

        output_fbo.release()
        output_tex.release()

    def test_r16ui_self_test_rejects_sampler2d_u2_path(
        self,
        yuv_uploader,
    ) -> None:
        """Verify that is_r16_available() is False before self-test passes.

        The old R16 path was enabled just because texture creation worked.
        Now is_r16_available() should be False until the self-test passes.
        """
        # Before self-test, R16UI should not be available
        assert not yuv_uploader.is_r16_available(), (
            "is_r16_available() should be False before self-test"
        )

    def test_upload_yuv_planar_falls_back_when_r16ui_self_test_fails(
        self,
        gl_context,
        monkeypatch,
    ) -> None:
        """upload_yuv_planar must fall back to float16 when R16UI self-test fails."""
        uploader = YUVGPUUploader(gl_context, pool_size=5)

        # 真正模拟"自测失败"：>8-bit 首次上传会触发 _auto_enable_r16ui()，它调用
        # run_r16ui_self_test()。若不强制其返回 False，在本机能创建 uint16 纹理的
        # GL 环境里自测会通过并走 r16ui_direct，无法验证回退路径。
        monkeypatch.setattr(uploader, "run_r16ui_self_test", lambda: False)
        assert not uploader.is_r16_available()

        # 12-bit upload should use float16 path
        y = np.full((4, 4), 2048, dtype=np.uint16)
        u = np.full((4, 4), 2048, dtype=np.uint16)
        v = np.full((4, 4), 2048, dtype=np.uint16)

        y_tex, u_tex, v_tex = uploader.upload_yuv_planar(
            y, u, v, frame_index=0, bit_depth=12, subsampling='444'
        )

        # Verify float16 path was used (not R16UI)
        timing = uploader.get_last_upload_timing()
        assert timing.get('mode', 'float16') != 'r16ui_direct', (
            "Should not use R16UI when self-test hasn't passed"
        )

        # Verify render produces correct colors
        output_tex = gl_context.texture((4, 4), components=4, dtype='f1')
        output_fbo = gl_context.framebuffer(color_attachments=[output_tex])

        uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
        uploader.set_color_range(YUVGPUUploader.RANGE_LIMITED)
        uploader.render_yuv_to_rgb(
            y_tex, u_tex, v_tex, output_fbo, bit_depth=12
        )

        pixels = np.frombuffer(
            output_fbo.read(components=4), dtype=np.uint8
        ).reshape(4, 4, 4)
        r, g, b, _ = pixels[0, 0]

        assert 120 <= int(r) <= 140, f"Float16 fallback R={r} not in [120, 140]"
        assert abs(int(r) - int(g)) <= 2

        output_fbo.release()
        output_tex.release()
        uploader.release()

    def test_upload_nv12(self, yuv_uploader) -> None:
        """上传 NV12"""
        y, uv = create_test_nv12_frame(1920, 1080, 8)

        y_tex, uv_tex = yuv_uploader.upload_nv12(y, uv, frame_index=0)

        assert y_tex is not None
        assert uv_tex is not None

        assert y_tex.size == (1920, 1080)
        assert uv_tex.size == (960, 540)  # NV12 UV 尺寸

    def test_upload_p010(self, yuv_uploader) -> None:
        """上传 P010 (10-bit NV12)"""
        y, uv = create_test_nv12_frame(1920, 1080, 10)

        y_tex, uv_tex = yuv_uploader.upload_p010(y, uv, frame_index=0)

        assert y_tex is not None
        assert uv_tex is not None

    def test_4k_upload(self, yuv_uploader) -> None:
        """4K 帧上传"""
        y, u, v = create_test_yuv_frame(3840, 2160, '420', 8)

        y_tex, u_tex, v_tex = yuv_uploader.upload_yuv420(y, u, v, frame_index=0)

        assert y_tex.size == (3840, 2160)
        assert u_tex.size == (1920, 1080)

    def test_yuv_matrix_setting(self, yuv_uploader) -> None:
        """YUV 矩阵设置"""
        yuv_uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)
        assert yuv_uploader._yuv_matrix == YUVGPUUploader.MATRIX_BT709

        yuv_uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT2020)
        assert yuv_uploader._yuv_matrix == YUVGPUUploader.MATRIX_BT2020

        yuv_uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT601)
        assert yuv_uploader._yuv_matrix == YUVGPUUploader.MATRIX_BT601

    def test_color_range_setting(self, yuv_uploader) -> None:
        """颜色范围设置"""
        yuv_uploader.set_color_range(YUVGPUUploader.RANGE_LIMITED)
        assert yuv_uploader._color_range == YUVGPUUploader.RANGE_LIMITED

        yuv_uploader.set_color_range(YUVGPUUploader.RANGE_FULL)
        assert yuv_uploader._color_range == YUVGPUUploader.RANGE_FULL

    def test_shader_creation(self, yuv_uploader) -> None:
        """Shader 创建"""
        shader = yuv_uploader.get_yuv_shader()

        assert shader is not None
        assert 'y_plane' in shader
        assert 'u_plane' in shader
        assert 'v_plane' in shader
        assert 'yuv_matrix' in shader
        assert 'color_range' in shader
        assert 'chroma_uv_scale' in shader
        assert 'chroma_uv_offset' in shader

    def test_nv12_shader_creation(self, yuv_uploader) -> None:
        """NV12 Shader 创建"""
        shader = yuv_uploader.get_nv12_shader()

        assert shader is not None
        assert 'y_plane' in shader
        assert 'uv_plane' in shader

    def test_nv12_shader_is_compiled_once_and_cached(self, yuv_uploader) -> None:
        """G-P0-01: get_nv12_shader() 必须只编译一次并缓存。

        此前每次调用都重新 ``self._ctx.program(...)``，既造成编译尖峰，
        又让 AsyncRenderer._render_quad 的 VAO cache（按 program.glo 索引）
        每帧新增一个永不释放的 VAO。这里验证：多次调用返回同一个 Program
        对象（同一 glo），且 ``_nv12_program`` 在首次取用后即固定。
        """
        first = yuv_uploader.get_nv12_shader()
        assert yuv_uploader._nv12_program is first

        # 模拟热路径连续取 shader（每帧一次），必须命中缓存、零新增编译。
        for _ in range(64):
            again = yuv_uploader.get_nv12_shader()
            assert again is first
            assert again.glo == first.glo

        # 缓存槽不应被覆盖：仍是首次编译得到的对象。
        assert yuv_uploader._nv12_program is first

    def test_nv12_shader_cache_released_on_cleanup(self, gl_context) -> None:
        """G-P0-01: release() 必须释放缓存的 NV12 sampler2D shader。

        随 uploader 生命周期统一释放，避免反复开关/换源后 GL 对象不回基线。
        """
        uploader = YUVGPUUploader(gl_context)
        shader = uploader.get_nv12_shader()
        assert shader is not None
        assert uploader._nv12_program is shader

        uploader.release()

        assert uploader._nv12_program is None

    def test_yuv_shaders_keep_direct_uv_and_transparent_bounds(self) -> None:
        """播放路径保持直接 UV 采样；越界透明以露出灰色画布背景。"""
        import inspect

        source = inspect.getsource(YUVGPUUploader)

        assert "vec2 viewport_uv = vec2(v_texcoord.x, 1.0 - v_texcoord.y);" not in source
        assert "vec2 source_uv = v_texcoord * source_uv_scale + source_uv_offset;" in source
        assert "f_color = vec4(0.0, 0.0, 0.0, 0.0);" in source
        assert "f_color = vec4(0.0, 0.0, 0.0, 1.0);" not in source

    def test_resource_cleanup(self, gl_context) -> None:
        """资源清理"""
        uploader = YUVGPUUploader(gl_context)

        # 上传几个帧
        y, u, v = create_test_yuv_frame(1920, 1080, '420', 8)
        uploader.upload_yuv420(y, u, v, frame_index=0)
        uploader.upload_yuv420(y, u, v, frame_index=1)

        # 释放资源
        uploader.release()

        stats = uploader.get_pool_stats()
        assert stats['y_pool_size'] == 0
        assert stats['uv_pool_size'] == 0


# =============================================================================
# Bandwidth Tests
# =============================================================================

class TestBandwidthReduction:
    """带宽减少测试"""

    def test_bandwidth_calculation(self) -> None:
        """带宽计算"""
        # 4K 分辨率
        width = 3840
        height = 2160

        # RGB 数据大小（float16）
        rgb_bytes = width * height * 3 * 2  # 3 通道 * 2 bytes (float16)

        # YUV420 数据大小（float16）
        yuv420_y_bytes = width * height * 2
        yuv420_uv_bytes = (width // 2) * (height // 2) * 2 * 2  # U + V
        yuv420_bytes = yuv420_y_bytes + yuv420_uv_bytes

        # 计算节省比例
        saved_ratio = (rgb_bytes - yuv420_bytes) / rgb_bytes

        # 验证约 87.5% 节省（理论值）
        # RGB: 3840 * 2160 * 3 * 2 = 49,766,400 bytes = ~47.4 MB
        # YUV420: 3840 * 2160 * 2 + 1920 * 1080 * 2 * 2 = 16,588,800 + 4,147,200 = ~20.0 MB
        # 节省: (47.4 - 20.0) / 47.4 = ~57.6%
        # 注意：实际计算使用 float16，节省比例不同于 uint8

        # 验证节省比例大于 50%
        assert saved_ratio >= 0.5, f"Expected bandwidth saved >= 0.5, got {saved_ratio}"

    def test_bandwidth_saved_method(self, yuv_uploader) -> None:
        """带宽节省方法测试"""
        if not HAS_YUV_MODULE:
            pytest.skip("YUV module not available")

        # 4K YUV420
        saved_420 = yuv_uploader.calculate_bandwidth_saved(3840, 2160, '420')
        assert saved_420 > 20  # 应节省超过 20 MB

        # 4K YUV422（节省较少）
        saved_422 = yuv_uploader.calculate_bandwidth_saved(3840, 2160, '422')
        assert saved_422 > 0

        # 4K YUV444（节省最少）
        saved_444 = yuv_uploader.calculate_bandwidth_saved(3840, 2160, '444')
        # YUV444 实际上数据量相同（如果使用 float16 纹理）

    def test_chroma_scale_function(self) -> None:
        """色度缩放函数（已废弃，返回默认值 1.0, 1.0）"""
        # 独立纹理不需要额外的 scale，UV 纹理坐标已归一化
        scale_420 = get_chroma_scale('420')
        assert scale_420 == (1.0, 1.0)

        scale_422 = get_chroma_scale('422')
        assert scale_422 == (1.0, 1.0)

        scale_444 = get_chroma_scale('444')
        assert scale_444 == (1.0, 1.0)

    def test_chroma_uv_params_function(self) -> None:
        """Chroma UV 参数函数"""
        scale, offset = get_chroma_uv_params('420')
        assert scale == (1.0, 1.0)
        assert offset == (0.0, 0.0)

        scale, offset = get_chroma_uv_params('422')
        assert scale == (1.0, 1.0)
        assert offset == (0.0, 0.0)

        scale, offset = get_chroma_uv_params('444')
        assert scale == (1.0, 1.0)
        assert offset == (0.0, 0.0)


# =============================================================================
# Helper Function Tests
# =============================================================================

class TestHelperFunctions:
    """辅助函数测试"""

    def test_create_test_yuv_frame_420(self) -> None:
        """创建 YUV420 测试帧"""
        y, u, v = create_test_yuv_frame(1920, 1080, '420', 8)

        assert y.shape == (1080, 1920)
        assert y.dtype == np.uint8

        assert u.shape == (540, 960)  # 420: 1/4 尺寸
        assert v.shape == (540, 960)

    def test_create_test_yuv_frame_10bit(self) -> None:
        """创建 10-bit 测试帧"""
        y, u, v = create_test_yuv_frame(1920, 1080, '420', 10)

        assert y.dtype == np.uint16
        assert y.max() <= 1023  # 10-bit 最大值

    def test_create_test_nv12_frame(self) -> None:
        """创建 NV12 测试帧"""
        y, uv = create_test_nv12_frame(1920, 1080, 8)

        assert y.shape == (1080, 1920)
        assert uv.shape == (540, 960, 2)  # interleaved UV

    def test_create_test_nv12_frame_10bit(self) -> None:
        """创建 10-bit NV12 测试帧"""
        y, uv = create_test_nv12_frame(1920, 1080, 10)

        assert y.dtype == np.uint16
        assert uv.dtype == np.uint16


# =============================================================================
# Performance Tests
# =============================================================================

@pytest.mark.slow
@pytest.mark.skipif(not HAS_MODERNGL or not HAS_YUV_MODULE, reason="moderngl or YUV module not available")
class TestPerformance:
    """性能测试"""

    def test_1080p_upload_time(self, gl_context) -> None:
        """1080p 上传时间"""
        uploader = YUVGPUUploader(gl_context)

        y, u, v = create_test_yuv_frame(1920, 1080, '420', 8)

        # 预热
        uploader.upload_yuv420(y, u, v, frame_index=0)

        # 测量
        start = time.time()
        for i in range(10):
            uploader.upload_yuv420(y, u, v, frame_index=i)
        elapsed = time.time() - start

        avg_time_ms = (elapsed / 10) * 1000

        # 上传应在 50ms 内完成（宽松阈值）
        assert avg_time_ms < 50, f"Upload took {avg_time_ms:.2f}ms per frame"

        uploader.release()

    def test_4k_upload_time(self, gl_context) -> None:
        """4K 上传时间"""
        uploader = YUVGPUUploader(gl_context)

        y, u, v = create_test_yuv_frame(3840, 2160, '420', 8)

        # 预热
        uploader.upload_yuv420(y, u, v, frame_index=0)

        # 测量
        start = time.time()
        for i in range(5):
            uploader.upload_yuv420(y, u, v, frame_index=i)
        elapsed = time.time() - start

        avg_time_ms = (elapsed / 5) * 1000

        # 4K 上传应在 150ms 内完成（float16 路径的 Python/Numpy 转换开销）
        assert avg_time_ms < 150, f"4K upload took {avg_time_ms:.2f}ms per frame"

        uploader.release()

    def test_upload_vs_rgb_comparison(self, gl_context) -> None:
        """YUV vs RGB 上传对比"""
        uploader = YUVGPUUploader(gl_context)

        # YUV420 4K 数据
        y, u, v = create_test_yuv_frame(3840, 2160, '420', 8)

        # RGB 4K 数据（模拟 CPU 端转换后的 RGB）
        rgb = np.random.rand(2160, 3840, 3).astype(np.float16)

        # YUV 上传
        start_yuv = time.time()
        uploader.upload_yuv420(y, u, v, frame_index=0)
        yuv_time = time.time() - start_yuv

        # RGB 上传（直接）
        rgb_tex = gl_context.texture((3840, 2160), 3, dtype='f2')
        start_rgb = time.time()
        rgb_tex.write(rgb.tobytes())
        rgb_time = time.time() - start_rgb

        # 统计带宽
        yuv_bytes = y.nbytes + u.nbytes + v.nbytes
        rgb_bytes = rgb.nbytes

        bandwidth_saved = (rgb_bytes - yuv_bytes) / rgb_bytes

        logger.info(f"YUV upload: {yuv_time*1000:.2f}ms, {yuv_bytes/1e6:.1f}MB")
        logger.info(f"RGB upload: {rgb_time*1000:.2f}ms, {rgb_bytes/1e6:.1f}MB")
        logger.info(f"Bandwidth saved: {bandwidth_saved*100:.1f}%")

        # 验证带宽节省
        assert bandwidth_saved >= 0.5, f"Expected bandwidth saved >= 0.5, got {bandwidth_saved}"

        uploader.release()
        rgb_tex.release()


# =============================================================================
# Context Manager Tests
# =============================================================================

@pytest.mark.skipif(not HAS_MODERNGL or not HAS_YUV_MODULE, reason="moderngl or YUV module not available")
class TestContextManager:
    """上下文管理器测试"""

    def test_with_statement(self, gl_context) -> None:
        """with 语句：上下文管理器内可正常上传，退出时自动释放。"""
        with YUVGPUUploader(gl_context) as uploader:
            y, u, v = create_test_yuv_frame(1920, 1080, '420', 8)
            y_tex, u_tex, v_tex = uploader.upload_yuv420(y, u, v, frame_index=0)

            # 8-bit 上传走独立的 R8 直传池（_r8_pool），不经 get_pool_stats() 报告的
            # float16 池。这里验证上传产出有效纹理，证明 uploader 在 with 块内可用。
            assert y_tex is not None
            assert u_tex is not None
            assert v_tex is not None

        # 退出 with 块触发 __exit__ → release()；能执行到这里说明清理无异常。

    def test_exception_handling(self, gl_context) -> None:
        """异常处理"""
        try:
            with YUVGPUUploader(gl_context) as uploader:
                y, u, v = create_test_yuv_frame(1920, 1080, '420', 8)
                uploader.upload_yuv420(y, u, v, frame_index=0)

                # 触发异常
                raise ValueError("Test exception")
        except ValueError:
            pass  # 预期异常

        # 资源应已清理（无法直接验证）


# =============================================================================
# Integration Tests
# =============================================================================

@pytest.mark.skipif(not HAS_MODERNGL or not HAS_YUV_MODULE, reason="moderngl or YUV module not available")
class TestIntegration:
    """集成测试"""

    def test_full_yuv_pipeline(self, gl_context) -> None:
        """完整 YUV 管线"""
        uploader = YUVGPUUploader(gl_context)

        # 创建测试帧
        y, u, v = create_test_yuv_frame(1920, 1080, '420', 8)

        # 上传 YUV 平面
        y_tex, u_tex, v_tex = uploader.upload_yuv420(y, u, v, frame_index=0)

        # 获取 shader
        shader = uploader.get_yuv_shader()

        # 创建输出 FBO
        output_tex = gl_context.texture((1920, 1080), 4, dtype='f2')
        output_fbo = gl_context.framebuffer([output_tex])

        # 渲染 YUV 到 RGB（需要 VAO）
        # 这里只验证创建，不实际渲染
        assert y_tex.glo > 0
        assert u_tex.glo > 0
        assert v_tex.glo > 0
        assert output_fbo.size == (1920, 1080)

        # 清理
        output_fbo.release()
        output_tex.release()
        uploader.release()

    def test_nv12_pipeline(self, gl_context) -> None:
        """NV12 管线"""
        uploader = YUVGPUUploader(gl_context)

        y, uv = create_test_nv12_frame(1920, 1080, 8)

        y_tex, uv_tex = uploader.upload_nv12(y, uv, frame_index=0)

        shader = uploader.get_nv12_shader()

        assert y_tex.glo > 0
        assert uv_tex.glo > 0

        uploader.release()

    def test_multiple_format_uploads(self, gl_context) -> None:
        """多格式上传"""
        uploader = YUVGPUUploader(gl_context)

        # 420
        y420, u420, v420 = create_test_yuv_frame(1920, 1080, '420', 8)
        uploader.upload_yuv420(y420, u420, v420, frame_index=0)

        # 422
        y422, u422, v422 = create_test_yuv_frame(1920, 1080, '422', 8)
        uploader.upload_yuv422(y422, u422, v422, frame_index=1)

        # NV12
        ynv12, uvnv12 = create_test_nv12_frame(1920, 1080, 8)
        uploader.upload_nv12(ynv12, uvnv12, frame_index=2)

        stats = uploader.get_upload_stats()
        assert stats.frame_count == 3

        uploader.release()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
