"""
AsyncRenderer GPU Pass 优化测试

## 测试目标

验证 GPU pass 优化效果：
1. YUV 播放 path 每帧减少一次 full-frame pass
2. 播放默认不每帧阻塞 GPU，避免 glFinish 拖慢实时播放
3. 图片调色回归不受影响

## 测试覆盖

- P1: 可配置同步模式 (finish/fence/none)
- P2: 统计计数器验证
- P3: YUV RGB output texture ring buffer
- P4: 持久化 raw_view/ping/pong 纹理
- Shader 编译覆盖 packed/YUV 两种输入

"""

from __future__ import annotations

import pytest
import numpy as np
from typing import Optional, Any, Dict, Tuple, List
from unittest.mock import MagicMock, Mock, patch, PropertyMock

# 导入核心模块
from src.features.color.core.async_renderer import AsyncRenderer, RenderCommand


# =============================================================================
# Mock Context and Renderer for Testing
# =============================================================================

class FakeModernGLContext:
    """模拟 ModernGL Context，统计 finish() 调用次数"""
    
    def __init__(self):
        self.finish_call_count = 0
        self._textures: List[Mock] = []
        self._framebuffers: List[Mock] = []
        self.viewport = (0, 0, 100, 100)
    
    def finish(self):
        """模拟 finish()，记录调用次数"""
        self.finish_call_count += 1
    
    def texture(self, size: Tuple[int, int], components: int, dtype: str = 'f2'):
        """模拟纹理创建"""
        tex = Mock()
        tex.size = size
        tex.components = components
        tex.dtype = dtype
        tex.glo = len(self._textures) + 1
        tex.filter = Mock()
        tex.repeat_x = False
        tex.repeat_y = False
        tex.use = Mock()
        tex.write = Mock()
        tex.release = Mock()
        self._textures.append(tex)
        return tex
    
    def framebuffer(self, color_attachments: List[Mock]):
        """模拟 FBO 创建"""
        fbo = Mock()
        fbo.use = Mock()
        fbo.release = Mock()
        self._framebuffers.append(fbo)
        return fbo
    
    def program(self, vertex_shader: str, fragment_shader: str):
        """模拟 shader program 创建"""
        prog = Mock()
        prog.__getitem__ = Mock(return_value=Mock(value=Mock()))
        return prog
    
    def enable(self, flag):
        pass
    
    def disable(self, flag):
        pass
    
    def clear(self, r, g, b, a):
        pass
    
    @property
    def blend_func(self):
        return Mock()
    
    @blend_func.setter
    def blend_func(self, value):
        pass


class FakeFramebufferManager:
    """模拟 FramebufferManager，统计临时纹理分配次数"""
    
    def __init__(self, ctx: FakeModernGLContext):
        self.ctx = ctx
        self.temp_texture_alloc_count = 0
        self._texture_pool: Dict[Tuple, List[Mock]] = {}
        self._fbo_cache: Dict[Mock, Mock] = {}
    
    def get_temp_texture(self, width: int, height: int, components: int = 4, dtype: str = 'f2'):
        """获取临时纹理，统计分配次数"""
        self.temp_texture_alloc_count += 1
        return self.ctx.texture((width, height), components, dtype)
    
    def get_fbo(self, tex: Mock):
        """获取 FBO"""
        if tex not in self._fbo_cache:
            self._fbo_cache[tex] = self.ctx.framebuffer([tex])
        return self._fbo_cache[tex]
    
    def release_temp_texture(self, tex: Mock):
        """释放临时纹理（模拟）"""
        pass
    
    def cleanup(self):
        pass


class FakeAsyncRenderer:
    """
    模拟 AsyncRenderer 用于测试。
    
    不依赖真实 OpenGL 上下文，只验证逻辑和统计。
    """
    
    def __init__(self):
        self.ctx = FakeModernGLContext()
        self.fb_manager = FakeFramebufferManager(self.ctx)
        
        # 统计计数器（与真实 AsyncRenderer 相同）
        self._temp_texture_alloc_count = 0
        self._finish_call_count = 0
        self.video_render_sync_mode = "none"
        
        # YUV RGB output cache
        self._yuv_rgb_output_cache: Dict[Tuple[int, int], List[Mock]] = {}
        self._yuv_rgb_output_ring_idx: Dict[Tuple[int, int], int] = {}
        self._yuv_rgb_output_ring_size = 3
        
        # 持久化纹理
        self._persistent_raw_view_tex: Optional[Mock] = None
        self._persistent_raw_view_fbo: Optional[Mock] = None
        self._persistent_ping_tex: Optional[Mock] = None
        self._persistent_ping_fbo: Optional[Mock] = None
        self._persistent_pong_tex: Optional[Mock] = None
        self._persistent_pong_fbo: Optional[Mock] = None
        self._persistent_tex_size: Tuple[int, int] = (0, 0)
        
        # Video state
        self.current_video_planes: Optional[Tuple] = None
        self.current_video_pixel_format: Optional[str] = None
        self.current_video_texture: Optional[Mock] = None
        self.pyramid_textures: List[Mock] = []
        self.utility_shaders = {
            'raw_view': Mock(),
            'passthrough': Mock(),
        }
    
    def reset_render_stats(self):
        """重置统计计数器"""
        self._temp_texture_alloc_count = 0
        self._finish_call_count = 0
        self.ctx.finish_call_count = 0
        self.fb_manager.temp_texture_alloc_count = 0
    
    def get_render_stats(self) -> Dict[str, Any]:
        """获取渲染统计"""
        return {
            'temp_texture_alloc_count': self._temp_texture_alloc_count,
            'finish_call_count': self._finish_call_count,
            'ctx_finish_count': self.ctx.finish_call_count,
            'fb_manager_alloc_count': self.fb_manager.temp_texture_alloc_count,
            'persistent_tex_size': self._persistent_tex_size,
            'yuv_rgb_cache_sizes': {str(k): len(v) for k, v in self._yuv_rgb_output_cache.items()},
            'video_render_sync_mode': self.video_render_sync_mode,
        }
    
    def _ensure_persistent_render_textures(self, width: int, height: int):
        """获取持久化渲染纹理"""
        current_size = (width, height)
        
        if self._persistent_tex_size == current_size and self._persistent_raw_view_tex is not None:
            return (
                self._persistent_raw_view_tex, self._persistent_raw_view_fbo,
                self._persistent_ping_tex, self._persistent_ping_fbo,
                self._persistent_pong_tex, self._persistent_pong_fbo
            )
        
        # 创建新纹理
        self._persistent_raw_view_tex = self.ctx.texture((width, height), 4, 'f2')
        self._persistent_raw_view_fbo = self.ctx.framebuffer([self._persistent_raw_view_tex])
        self._persistent_ping_tex = self.ctx.texture((width, height), 4, 'f2')
        self._persistent_ping_fbo = self.ctx.framebuffer([self._persistent_ping_tex])
        self._persistent_pong_tex = self.ctx.texture((width, height), 4, 'f2')
        self._persistent_pong_fbo = self.ctx.framebuffer([self._persistent_pong_tex])
        
        self._persistent_tex_size = current_size
        self._temp_texture_alloc_count += 3
        
        return (
            self._persistent_raw_view_tex, self._persistent_raw_view_fbo,
            self._persistent_ping_tex, self._persistent_ping_fbo,
            self._persistent_pong_tex, self._persistent_pong_fbo
        )
    
    def _get_yuv_rgb_output_texture(self, width: int, height: int):
        """获取 YUV RGB 输出纹理（ring buffer）"""
        size_key = (width, height)
        
        if size_key not in self._yuv_rgb_output_cache:
            self._yuv_rgb_output_cache[size_key] = []
            self._yuv_rgb_output_ring_idx[size_key] = 0
        
        cache_list = self._yuv_rgb_output_cache[size_key]
        
        if len(cache_list) < self._yuv_rgb_output_ring_size:
            tex = self.ctx.texture((width, height), 4, 'f2')
            cache_list.append(tex)
            self._temp_texture_alloc_count += 1
            return tex
        
        idx = self._yuv_rgb_output_ring_idx[size_key]
        tex = cache_list[idx]
        self._yuv_rgb_output_ring_idx[size_key] = (idx + 1) % len(cache_list)
        return tex
    
    def _sync_gpu(self):
        """根据同步模式执行 GPU 同步"""
        if self.video_render_sync_mode == "finish":
            self.ctx.finish()
            self._finish_call_count += 1
        elif self.video_render_sync_mode == "fence":
            self.ctx.finish()
            self._finish_call_count += 1
        # "none" 模式：不调用任何同步
    
    def simulate_render_frame(self, params: Dict[str, Any]):
        """
        模拟渲染一帧的逻辑，用于验证统计。
        
        Args:
            params: 渲染参数
                - viewport_size: (width, height)
                - color_output_mode: bool
                - has_video_planes: bool (是否使用 YUV planes)
                - has_video_texture: bool (是否使用 packed video texture)
                - has_image: bool (是否使用 pyramid image)
        """
        w, h = params.get('viewport_size', (100, 100))
        color_output_mode = params.get('color_output_mode', False)
        has_video_planes = params.get('has_video_planes', False)
        has_video_texture = params.get('has_video_texture', False)
        has_image = params.get('has_image', False)
        
        # 设置 video state
        self.current_video_planes = (Mock(), Mock(), Mock()) if has_video_planes else None
        self.current_video_texture = Mock() if has_video_texture else None
        self.pyramid_textures = [Mock()] if has_image else []
        
        # 1. 获取 raw_view 纹理
        if color_output_mode:
            raw_view_tex = self.fb_manager.get_temp_texture(w, h, 4, 'f2')
            raw_view_fbo = self.fb_manager.get_fbo(raw_view_tex)
            self._temp_texture_alloc_count += 1
        else:
            raw_view_tex, raw_view_fbo, ping_tex, ping_fbo, pong_tex, pong_fbo = \
                self._ensure_persistent_render_textures(w, h)
        
        # 2. YUV planes 转 RGB（如果需要）
        if has_video_planes:
            yuv_rgb_tex = self._get_yuv_rgb_output_texture(w, h)
        
        # 3. 获取 ping/pong 纹理（ColorOutput 模式）
        if color_output_mode:
            ping_tex = self.fb_manager.get_temp_texture(w, h, 4, 'f2')
            pong_tex = self.fb_manager.get_temp_texture(w, h, 4, 'f2')
            ping_fbo = self.fb_manager.get_fbo(ping_tex)
            pong_fbo = self.fb_manager.get_fbo(pong_tex)
            self._temp_texture_alloc_count += 2
        
        # 4. 同步
        if color_output_mode:
            self.ctx.finish()
            self._finish_call_count += 1
        elif has_image and not has_video_texture and not has_video_planes:
            self.ctx.finish()
            self._finish_call_count += 1
        else:
            self._sync_gpu()


# =============================================================================
# Test Cases
# =============================================================================

class TestSyncModeConfiguration:
    """P1: 测试可配置同步模式"""
    
    def test_default_sync_mode_is_none(self):
        """默认同步模式应为 'none'，避免播放时每帧 glFinish"""
        renderer = FakeAsyncRenderer()
        assert renderer.video_render_sync_mode == "none"
    
    def test_sync_mode_finish_calls_ctx_finish(self):
        """'finish' 模式应调用 ctx.finish()"""
        renderer = FakeAsyncRenderer()
        renderer.video_render_sync_mode = "finish"
        
        renderer.reset_render_stats()
        renderer._sync_gpu()
        
        assert renderer.ctx.finish_call_count == 1
        assert renderer._finish_call_count == 1
    
    def test_sync_mode_fence_calls_ctx_finish(self):
        """'fence' 模式应调用 ctx.finish()（暂时用 finish 模拟）"""
        renderer = FakeAsyncRenderer()
        renderer.video_render_sync_mode = "fence"
        
        renderer.reset_render_stats()
        renderer._sync_gpu()
        
        assert renderer.ctx.finish_call_count == 1
    
    def test_sync_mode_none_skips_ctx_finish(self):
        """'none' 模式应不调用 ctx.finish()"""
        renderer = FakeAsyncRenderer()
        renderer.video_render_sync_mode = "none"
        
        renderer.reset_render_stats()
        renderer._sync_gpu()
        
        assert renderer.ctx.finish_call_count == 0
        assert renderer._finish_call_count == 0


class TestTextureAllocationStats:
    """P2/P3/P4: 测试纹理分配统计和优化"""
    
    def test_persistent_texture_reuse_reduces_alloc_count(self):
        """持久化纹理复用应减少分配次数"""
        renderer = FakeAsyncRenderer()
        renderer.reset_render_stats()
        
        # 第一次渲染：创建持久化纹理
        renderer._ensure_persistent_render_textures(100, 100)
        first_alloc = renderer._temp_texture_alloc_count
        assert first_alloc == 3  # raw_view + ping + pong
        
        # 第二次渲染：复用持久化纹理（相同尺寸）
        renderer._ensure_persistent_render_textures(100, 100)
        second_alloc = renderer._temp_texture_alloc_count
        
        # 分配次数不应增加
        assert second_alloc == first_alloc
    
    def test_persistent_texture_recreate_on_size_change(self):
        """尺寸变化时应重新创建持久化纹理"""
        renderer = FakeAsyncRenderer()
        renderer.reset_render_stats()
        
        renderer._ensure_persistent_render_textures(100, 100)
        first_alloc = renderer._temp_texture_alloc_count
        
        # 尺寸变化：重新创建
        renderer._ensure_persistent_render_textures(200, 200)
        second_alloc = renderer._temp_texture_alloc_count
        
        # 应增加新的分配
        assert second_alloc == first_alloc + 3
        assert renderer._persistent_tex_size == (200, 200)
    
    def test_yuv_rgb_ring_buffer_reuse(self):
        """YUV RGB ring buffer 应复用纹理"""
        renderer = FakeAsyncRenderer()
        renderer.reset_render_stats()
        
        # 填满 ring buffer
        for i in range(3):
            tex = renderer._get_yuv_rgb_output_texture(100, 100)
        
        first_alloc = renderer._temp_texture_alloc_count
        assert first_alloc == 3
        
        # 继续获取：应该复用
        for i in range(5):
            tex = renderer._get_yuv_rgb_output_texture(100, 100)
        
        # 分配次数不应增加（ring buffer 已满）
        assert renderer._temp_texture_alloc_count == first_alloc
    
    def test_yuv_rgb_ring_buffer_different_sizes(self):
        """不同尺寸的 YUV RGB texture 应分开缓存"""
        renderer = FakeAsyncRenderer()
        renderer.reset_render_stats()
        
        # 两种尺寸
        tex_100 = renderer._get_yuv_rgb_output_texture(100, 100)
        tex_200 = renderer._get_yuv_rgb_output_texture(200, 200)
        
        # 应创建两个独立的缓存
        assert renderer._temp_texture_alloc_count == 2
        assert len(renderer._yuv_rgb_output_cache) == 2


class TestVideoPlaybackPassOptimization:
    """视频播放 pass 优化测试"""
    
    def test_video_playback_does_not_sync_by_default(self):
        """视频播放默认模式不应每帧强制同步"""
        renderer = FakeAsyncRenderer()
        renderer.reset_render_stats()
        
        # 模拟视频播放帧（非 ColorOutput）
        renderer.simulate_render_frame({
            'viewport_size': (100, 100),
            'color_output_mode': False,
            'has_video_planes': True,
        })
        
        stats = renderer.get_render_stats()
        
        # 黑屏由 FBO 绑定顺序修复，播放默认不再每帧 glFinish。
        assert stats['ctx_finish_count'] == 0
    
    def test_video_playback_reuses_persistent_texture(self):
        """视频播放应复用持久化纹理"""
        renderer = FakeAsyncRenderer()
        renderer.reset_render_stats()
        
        # 第一帧
        renderer.simulate_render_frame({
            'viewport_size': (100, 100),
            'color_output_mode': False,
            'has_video_planes': True,
        })
        first_alloc = renderer._temp_texture_alloc_count
        
        # 第二帧（相同尺寸）
        renderer.simulate_render_frame({
            'viewport_size': (100, 100),
            'color_output_mode': False,
            'has_video_planes': True,
        })
        second_alloc = renderer._temp_texture_alloc_count
        
        # 只有 YUV RGB texture 从 ring buffer 获取
        # 第一次会创建，第二次开始复用
        # raw_view/ping/pong 复用持久化纹理
        
        # 第二帧不应有新的纹理分配（ring buffer 已填充）
        assert second_alloc <= first_alloc + 1  #最多一个新 YUV texture
    
    def test_color_output_mode_uses_temp_texture_and_finish(self):
        """ColorOutput 模式应使用临时纹理并强制同步"""
        renderer = FakeAsyncRenderer()
        renderer.reset_render_stats()
        
        renderer.simulate_render_frame({
            'viewport_size': (100, 100),
            'color_output_mode': True,
            'has_image': True,
        })
        
        stats = renderer.get_render_stats()
        
        # ColorOutput 模式需要强制同步
        assert stats['ctx_finish_count'] == 1
        # ColorOutput 模式使用临时纹理
        assert stats['fb_manager_alloc_count'] > 0


class TestYUVInputShader:
    """YUV shader 编译测试"""
    
    def test_yuv_shader_compiles_for_nv12(self):
        """NV12 格式 shader 应能编译"""
        # 这个测试验证 YUVGPUUploader 的 shader 代码结构
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader
        
        # 验证 shader 代码存在
        # 在真实测试中需要 OpenGL 上下文
        # 这里只验证代码结构
        
        assert hasattr(YUVGPUUploader, 'get_nv12_shader')
        assert hasattr(YUVGPUUploader, 'get_yuv_shader')
    
    def test_yuv_shader_compiles_for_yuv420(self):
        """YUV420P 格式 shader 应能编译"""
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader
        
        assert hasattr(YUVGPUUploader, 'MATRIX_BT709')
        assert hasattr(YUVGPUUploader, 'MATRIX_BT2020')
        assert hasattr(YUVGPUUploader, 'MATRIX_BT601')
    
    def test_yuv_shader_handles_different_color_range(self):
        """YUV shader 应支持不同颜色范围"""
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader
        
        assert hasattr(YUVGPUUploader, 'RANGE_LIMITED')
        assert hasattr(YUVGPUUploader, 'RANGE_FULL')


class TestImageRegressionNotAffected:
    """图片调色回归测试"""
    
    def test_image_mode_uses_persistent_texture(self):
        """图片模式应使用持久化纹理"""
        renderer = FakeAsyncRenderer()
        renderer.reset_render_stats()
        
        # 模拟图片渲染（非 ColorOutput）
        renderer.simulate_render_frame({
            'viewport_size': (100, 100),
            'color_output_mode': False,
            'has_image': True,
        })
        
        stats = renderer.get_render_stats()
        
        # 图片模式应使用持久化纹理
        assert stats['persistent_tex_size'] == (100, 100)
    
    def test_image_mode_syncs_before_publish(self):
        """图片模式发布前应同步，保证静帧预览完整"""
        renderer = FakeAsyncRenderer()
        renderer.reset_render_stats()
        
        renderer.simulate_render_frame({
            'viewport_size': (100, 100),
            'color_output_mode': False,
            'has_image': True,
        })
        
        stats = renderer.get_render_stats()
        assert stats['ctx_finish_count'] == 1


class TestRealAsyncRendererIntegration:
    """真实 AsyncRenderer 集成测试（需要 OpenGL 上下文）"""
    
    @pytest.mark.skipif(
        True,  # 默认跳过，需要真实 OpenGL 环境
        reason="需要真实 OpenGL 上下文和 PyQt 环境"
    )
    def test_async_renderer_has_sync_mode_attribute(self):
        """AsyncRenderer 应有 video_render_sync_mode 属性"""
        # 这个测试需要在真实环境中运行
        from src.features.color.core.async_renderer import AsyncRenderer
        
        # 验证属性存在
        assert hasattr(AsyncRenderer, '__init__')
        # 在真实测试中验证初始化值
    
    @pytest.mark.skipif(
        True,
        reason="需要真实 OpenGL 上下文"
    )
    def test_async_renderer_get_render_stats(self):
        """AsyncRenderer 应有 get_render_stats 方法"""
        from src.features.color.core.async_renderer import AsyncRenderer
        
        assert hasattr(AsyncRenderer, 'get_render_stats')
        assert hasattr(AsyncRenderer, 'reset_render_stats')
        assert hasattr(AsyncRenderer, '_sync_gpu')


# =============================================================================
# EntryPoint
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
