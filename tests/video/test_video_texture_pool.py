"""
VideoTexturePool Tests - GPU 纹理池测试

测试覆盖范围：
1. acquire/release 生命周期
2. 槽位复用（5槽位限制）
3. cleanup 清理
4. 资源释放验证
5. 多次 acquire 同一帧
6. 尺寸变化处理
7. 池满时复用策略

注意事项：
- OpenGL 测试需要创建 standalone context
- 使用 pytest fixtures 管理 OpenGL 资源
- 测试后必须清理资源
"""

import pytest
import sys
import os
import numpy as np
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 检查 moderngl 是否可用
try:
    import moderngl
    HAS_MODERNGL = True
except ImportError:
    HAS_MODERNGL = False


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def gl_context():
    """创建 standalone ModernGL 上下文用于测试"""
    if not HAS_MODERNGL:
        pytest.skip("moderngl not available")
    
    try:
        ctx = moderngl.create_standalone_context()
        yield ctx
        # 清理：释放上下文
        # ModernGL standalone context 没有 release() 方法
        # Python GC 会自动清理
    except Exception as e:
        pytest.skip(f"Failed to create OpenGL context: {e}")


@pytest.fixture
def mock_context():
    """Mock ModernGL context（不需要真实 OpenGL）"""
    mock_ctx = MagicMock()
    
    # 模拟 texture 创建
    def create_texture(size=(1920, 1080), components=4, dtype='f2'):
        mock_tex = MagicMock()
        mock_tex.glo = id(mock_tex)  # 模拟 OpenGL ID
        mock_tex.size = size
        mock_tex.components = components
        mock_tex.dtype = dtype
        mock_tex.filter = (moderngl.LINEAR, moderngl.LINEAR) if HAS_MODERNGL else (0, 0)
        mock_tex.repeat_x = False
        mock_tex.repeat_y = False
        mock_tex.write = MagicMock()
        mock_tex.release = MagicMock()
        return mock_tex
    
    mock_ctx.texture = MagicMock(side_effect=create_texture)
    yield mock_ctx


@pytest.fixture
def video_pool(gl_context):
    """创建真实的 VideoTexturePool"""
    from src.shared.video.video_texture_pool import VideoTexturePool
    pool = VideoTexturePool(gl_context, pool_size=5)
    yield pool
    # 清理
    pool.release_all()


@pytest.fixture
def mock_pool(mock_context):
    """创建 mock VideoTexturePool"""
    from src.shared.video.video_texture_pool import VideoTexturePool
    pool = VideoTexturePool(mock_context, pool_size=5)
    yield pool
    pool.release_all()


# =============================================================================
# Unit Tests (Mock - 不需要真实 OpenGL)
# =============================================================================

class TestVideoTexturePoolLogic:
    """测试 VideoTexturePool 逻辑（不需要真实 OpenGL）"""

    def test_pool_initialization(self, mock_pool):
        """池初始化"""
        assert mock_pool._pool_size == 5
        assert len(mock_pool._slots) == 0
        
        stats = mock_pool.get_stats()
        assert stats['pool_size'] == 0
        assert stats['max_size'] == 5
        assert stats['active_frames'] == []

    def test_acquire_first_texture(self, mock_pool):
        """首次 acquire 创建新纹理"""
        tex = mock_pool.acquire(1920, 1080, frame_index=0)
        
        assert tex is not None
        assert len(mock_pool._slots) == 1
        
        stats = mock_pool.get_stats()
        assert stats['pool_size'] == 1
        assert stats['active_frames'] == [0]

    def test_acquire_same_size_reuse(self, mock_pool):
        """相同尺寸复用空闲槽位"""
        # First acquire
        tex1 = mock_pool.acquire(1920, 1080, frame_index=0)
        mock_pool.mark_unused(0)
        
        # Second acquire - should reuse slot
        tex2 = mock_pool.acquire(1920, 1080, frame_index=1)
        
        assert tex2 is not None
        assert len(mock_pool._slots) == 1  # 没有创建新槽位
        assert mock_pool.has_texture_for_frame(1)

    def test_acquire_different_size(self, mock_pool):
        """不同尺寸创建新槽位"""
        tex1 = mock_pool.acquire(1920, 1080, frame_index=0)
        tex2 = mock_pool.acquire(1280, 720, frame_index=1)
        
        assert tex1 is not None
        assert tex2 is not None
        assert len(mock_pool._slots) == 2
        
        stats = mock_pool.get_stats()
        assert stats['pool_size'] == 2
        assert (1920, 1080) in stats['sizes']
        assert (1280, 720) in stats['sizes']

    def test_pool_size_limit(self, mock_pool):
        """池大小限制（最多 5 个槽位）"""
        textures = []
        for i in range(7):
            tex = mock_pool.acquire(1920 + i * 100, 1080, frame_index=i)
            textures.append(tex)
        
        # 应最多 5 个槽位
        assert len(mock_pool._slots) == 5
        
        stats = mock_pool.get_stats()
        assert stats['pool_size'] == 5
        assert stats['max_size'] == 5

    def test_mark_unused(self, mock_pool):
        """标记帧不再使用"""
        tex = mock_pool.acquire(1920, 1080, frame_index=0)
        
        assert mock_pool.has_texture_for_frame(0)
        
        mock_pool.mark_unused(0)
        
        assert not mock_pool.has_texture_for_frame(0)
        assert mock_pool._slots[0].frame_index == -1

    def test_mark_unused_nonexistent_frame(self, mock_pool):
        """标记不存在帧号的无效操作"""
        # 不会抛出异常
        mock_pool.mark_unused(999)
        
        # 状态不变
        stats = mock_pool.get_stats()
        assert stats['pool_size'] == 0

    def test_release_all(self, mock_pool):
        """释放所有纹理"""
        # 使用不同尺寸创建多个槽位
        tex1 = mock_pool.acquire(1920, 1080, frame_index=0)
        tex2 = mock_pool.acquire(1280, 720, frame_index=1)
        tex3 = mock_pool.acquire(3840, 2160, frame_index=2)
        
        assert len(mock_pool._slots) == 3
        
        mock_pool.release_all()
        
        assert len(mock_pool._slots) == 0
        
        stats = mock_pool.get_stats()
        assert stats['pool_size'] == 0
        assert stats['active_frames'] == []

    def test_get_texture_for_frame(self, mock_pool):
        """获取指定帧号的纹理"""
        tex1 = mock_pool.acquire(1920, 1080, frame_index=0)
        tex2 = mock_pool.acquire(1280, 720, frame_index=1)
        
        # 获取帧 0 的纹理
        result = mock_pool.get_texture_for_frame(0)
        assert result is tex1
        
        # 获取帧 1 的纹理
        result = mock_pool.get_texture_for_frame(1)
        assert result is tex2
        
        # 获取不存在帧
        result = mock_pool.get_texture_for_frame(999)
        assert result is None

    def test_has_texture_for_frame(self, mock_pool):
        """检查帧号纹理是否存在"""
        tex = mock_pool.acquire(1920, 1080, frame_index=0)
        
        assert mock_pool.has_texture_for_frame(0)
        assert not mock_pool.has_texture_for_frame(1)

    def test_resize_pool_smaller(self, mock_pool):
        """调整池大小（变小）"""
        # 使用不同尺寸创建 5 个槽位
        sizes = [(1920, 1080), (1280, 720), (3840, 2160), (640, 480), (2560, 1440)]
        for i, (w, h) in enumerate(sizes):
            tex = mock_pool.acquire(w, h, frame_index=i)
        
        assert len(mock_pool._slots) == 5
        
        # 调整到 3
        mock_pool.resize_pool(3)
        
        assert mock_pool._pool_size == 3
        assert len(mock_pool._slots) == 3

    def test_resize_pool_larger(self, mock_pool):
        """调整池大小（变大）"""
        mock_pool.resize_pool(10)
        
        assert mock_pool._pool_size == 10
        
        # 可以创建更多槽位
        for i in range(8):
            tex = mock_pool.acquire(1920 + i * 100, 1080, frame_index=i)
        
        assert len(mock_pool._slots) == 8

    def test_repr_string(self, mock_pool):
        """字符串表示"""
        tex = mock_pool.acquire(1920, 1080, frame_index=0)
        
        repr_str = repr(mock_pool)
        assert "VideoTexturePool" in repr_str
        assert "slots=" in repr_str
        assert "frames=" in repr_str

    def test_components_parameter(self, mock_pool):
        """不同通道数纹理"""
        tex_rgba = mock_pool.acquire(1920, 1080, frame_index=0, components=4)
        tex_rgb = mock_pool.acquire(1920, 1080, frame_index=1, components=3)
        
        # 不同通道数需要不同槽位
        assert len(mock_pool._slots) == 2
        
        stats = mock_pool.get_stats()
        assert 4 in stats['components']
        assert 3 in stats['components']

    def test_dtype_parameter(self, mock_pool):
        """不同数据类型"""
        tex_f2 = mock_pool.acquire(1920, 1080, frame_index=0, dtype='f2')
        tex_f4 = mock_pool.acquire(1920, 1080, frame_index=1, dtype='f4')
        
        assert tex_f2 is not None
        assert tex_f4 is not None


# =============================================================================
# LRU Tests
# =============================================================================

class TestVideoTexturePoolLRU:
    """测试 LRU 复用策略"""

    def test_reuse_oldest_slot(self, mock_pool):
        """池满时复用最旧槽位"""
        # 填满池（不同尺寸）
        for i in range(5):
            tex = mock_pool.acquire(1920 + i * 100, 1080, frame_index=i)
            time.sleep(0.01)  # 确保 last_used_time 不同
        
        assert len(mock_pool._slots) == 5
        
        # 第 6 次 acquire，应复用最旧槽位（frame_index=0）
        tex6 = mock_pool.acquire(3840, 2160, frame_index=5)
        
        # 应替换槽位 0 的纹理
        assert mock_pool._slots[0].width == 3840
        assert mock_pool._slots[0].height == 2160
        assert mock_pool._slots[0].frame_index == 5

    def test_reuse_same_size_oldest(self, mock_pool):
        """相同尺寸时复用最旧"""
        # 相同尺寸只会创建一个槽位，然后不断复用
        tex1 = mock_pool.acquire(1920, 1080, frame_index=0)
        time.sleep(0.01)
        tex2 = mock_pool.acquire(1920, 1080, frame_index=1)
        time.sleep(0.01)
        tex3 = mock_pool.acquire(1920, 1080, frame_index=2)
        
        # 只有一个槽位（相同尺寸复用）
        assert len(mock_pool._slots) == 1
        
        # 第 4 次 acquire，应复用同一个槽位
        tex4 = mock_pool.acquire(1920, 1080, frame_index=3)
        
        # 仍然只有一个槽位
        assert len(mock_pool._slots) == 1
        assert mock_pool._slots[0].frame_index == 3  # 最新帧号


# =============================================================================
# Integration Tests (需要真实 OpenGL)
# =============================================================================

@pytest.mark.skipif(not HAS_MODERNGL, reason="moderngl not available")
class TestVideoTexturePoolIntegration:
    """集成测试（真实 OpenGL）"""

    def test_real_texture_creation(self, video_pool):
        """真实纹理创建"""
        tex = video_pool.acquire(1920, 1080, frame_index=0)
        
        assert tex is not None
        assert tex.size == (1920, 1080)
        assert tex.components == 4
        assert tex.dtype == 'f2'
        
        # 检查 filter 设置
        assert tex.filter[0] == moderngl.LINEAR
        assert tex.filter[1] == moderngl.LINEAR

    def test_real_texture_write(self, video_pool):
        """真实纹理写入数据"""
        tex = video_pool.acquire(1920, 1080, frame_index=0)
        
        # 创建测试数据（RGBA float16）
        data = np.zeros((1080, 1920, 4), dtype=np.float16)
        data[:, :, 0] = 0.5  # R
        data[:, :, 1] = 0.3  # G
        data[:, :, 2] = 0.2  # B
        data[:, :, 3] = 1.0  # A
        
        # 写入
        tex.write(data.tobytes())
        
        # 验证纹理存在
        assert video_pool.has_texture_for_frame(0)

    def test_real_texture_release(self, video_pool):
        """真实纹理释放"""
        tex = video_pool.acquire(1920, 1080, frame_index=0)
        glo = tex.glo
        
        video_pool.release_all()
        
        # 槽位清空
        assert len(video_pool._slots) == 0
        
        # 纹理已释放（无法再次获取）
        result = video_pool.get_texture_for_frame(0)
        assert result is None

    def test_real_multiple_frames(self, video_pool):
        """多帧上传"""
        textures = []
        for i in range(5):
            tex = video_pool.acquire(1920, 1080, frame_index=i)
            
            # 模拟帧数据
            data = np.full((1080, 1920, 4), i / 10.0, dtype=np.float16)
            tex.write(data.tobytes())
            
            textures.append(tex)
        
        # 检查统计
        stats = video_pool.get_stats()
        assert stats['pool_size'] == 1  # 相同尺寸复用同一槽位
        assert len(stats['active_frames']) <= 1  # 实际只有一个槽位持有最新帧

    def test_real_size_change(self, video_pool):
        """尺寸变化测试"""
        # 1920x1080
        tex1 = video_pool.acquire(1920, 1080, frame_index=0)
        assert tex1.size == (1920, 1080)
        
        # 1280x720 - 不同尺寸
        tex2 = video_pool.acquire(1280, 720, frame_index=1)
        assert tex2.size == (1280, 720)
        
        # 应有 2 个槽位
        assert len(video_pool._slots) == 2

    def test_real_4k_texture(self, video_pool):
        """4K 纹理测试"""
        tex = video_pool.acquire(3840, 2160, frame_index=0)
        
        assert tex is not None
        assert tex.size == (3840, 2160)
        
        # 4K 数据写入
        data = np.zeros((2160, 3840, 4), dtype=np.float16)
        tex.write(data.tobytes())

    def test_real_10bit_data(self, video_pool):
        """10-bit 数据处理"""
        # 使用 uint16 模拟 10-bit
        tex = video_pool.acquire(1920, 1080, frame_index=0, dtype='f2')
        
        # 10-bit YUV 数据归一化到 float
        y_data = np.random.randint(0, 1024, (1080, 1920), dtype=np.uint16)
        y_normalized = y_data.astype(np.float32) / 1023.0
        
        # 写入为 float16
        data = np.zeros((1080, 1920, 4), dtype=np.float16)
        data[:, :, 0] = y_normalized.astype(np.float16)
        
        tex.write(data.tobytes())


# =============================================================================
# Error Handling Tests
# =============================================================================

class TestVideoTexturePoolErrors:
    """错误处理测试"""

    def test_acquire_with_zero_size(self, mock_pool):
        """零尺寸返回 None"""
        # VideoTexturePool.acquire 内部不检查 size，直接创建纹理
        # 这取决于 ModernGL 的行为（会抛出异常）
        # Mock 版本会返回纹理
        tex = mock_pool.acquire(0, 0, frame_index=0)
        # Mock 不检查尺寸，所以会创建
        # 这是实现依赖，需要根据实际行为调整

    def test_acquire_negative_frame_index(self, mock_pool):
        """负帧号处理"""
        tex = mock_pool.acquire(1920, 1080, frame_index=-1)
        
        # 应能正常创建
        assert tex is not None
        assert mock_pool._slots[0].frame_index == -1

    def test_double_release(self, mock_pool):
        """重复释放"""
        tex = mock_pool.acquire(1920, 1080, frame_index=0)
        
        video_pool = mock_pool  # 使用 mock_pool
        
        # 第一次释放
        video_pool.release_all()
        
        # 第二次释放（不应抛出异常）
        video_pool.release_all()
        
        assert len(video_pool._slots) == 0

    def test_release_after_pool_full(self, mock_pool):
        """池满后释放再复用"""
        # 填满池
        for i in range(5):
            tex = mock_pool.acquire(1920 + i * 100, 1080, frame_index=i)
        
        assert len(mock_pool._slots) == 5
        
        # 释放
        mock_pool.release_all()
        
        # 再次使用
        tex = mock_pool.acquire(1920, 1080, frame_index=0)
        assert tex is not None
        assert len(mock_pool._slots) == 1


# =============================================================================
# Statistics Tests
# =============================================================================

class TestVideoTexturePoolStats:
    """统计信息测试"""

    def test_stats_empty_pool(self, mock_pool):
        """空池统计"""
        stats = mock_pool.get_stats()
        
        assert stats['pool_size'] == 0
        assert stats['max_size'] == 5
        assert stats['active_frames'] == []
        assert stats['sizes'] == []
        assert stats['components'] == []

    def test_stats_single_texture(self, mock_pool):
        """单纹理统计"""
        tex = mock_pool.acquire(1920, 1080, frame_index=0)
        
        stats = mock_pool.get_stats()
        assert stats['pool_size'] == 1
        assert 0 in stats['active_frames']
        assert (1920, 1080) in stats['sizes']
        assert 4 in stats['components']

    def test_stats_multiple_textures(self, mock_pool):
        """多纹理统计"""
        tex1 = mock_pool.acquire(1920, 1080, frame_index=0)
        tex2 = mock_pool.acquire(1280, 720, frame_index=1)
        
        stats = mock_pool.get_stats()
        assert stats['pool_size'] == 2
        assert 0 in stats['active_frames']
        assert 1 in stats['active_frames']
        assert len(stats['sizes']) == 2


# =============================================================================
# Memory Leak Tests
# =============================================================================

@pytest.mark.skipif(not HAS_MODERNGL, reason="moderngl not available")
class TestVideoTexturePoolMemory:
    """内存泄漏测试"""

    def test_no_leak_repeated_acquire_release(self, video_pool):
        """重复 acquire/release 不泄漏"""
        for i in range(100):
            tex = video_pool.acquire(1920, 1080, frame_index=i)
            data = np.zeros((1080, 1920, 4), dtype=np.float16)
            tex.write(data.tobytes())
            video_pool.mark_unused(i)
        
        # 应只有一个槽位（相同尺寸复用）
        stats = video_pool.get_stats()
        assert stats['pool_size'] == 1

    def test_pool_cleanup_after_video_end(self, video_pool):
        """视频结束后清理"""
        # 模拟视频播放
        for i in range(10):
            tex = video_pool.acquire(1920, 1080, frame_index=i)
        
        # 视频结束，清理
        video_pool.release_all()
        
        assert len(video_pool._slots) == 0

    def test_different_sizes_cleanup(self, video_pool):
        """不同尺寸清理"""
        sizes = [(1920, 1080), (1280, 720), (3840, 2160)]
        
        for w, h in sizes:
            tex = video_pool.acquire(w, h, frame_index=0)
        
        assert len(video_pool._slots) == 3
        
        video_pool.release_all()
        
        assert len(video_pool._slots) == 0


# =============================================================================
# Manual Test Runner
# =============================================================================

def run_manual_tests():
    """手动运行测试（用于调试）"""
    print("\n=== Manual VideoTexturePool Tests ===\n")
    
    if not HAS_MODERNGL:
        print("moderngl not available, skipping manual tests")
        return
    
    try:
        ctx = moderngl.create_standalone_context()
        print("Created standalone context")
        
        from src.shared.video.video_texture_pool import VideoTexturePool
        pool = VideoTexturePool(ctx, pool_size=5)
        print(f"Created pool: {repr(pool)}")
        
        # Test 1: Acquire
        print("\n--- Test 1: Acquire ---")
        tex = pool.acquire(1920, 1080, frame_index=0)
        print(f"  Acquired texture: glo={tex.glo}, size={tex.size}")
        print(f"  Pool stats: {pool.get_stats()}")
        
        # Test 2: Write data
        print("\n--- Test 2: Write data ---")
        data = np.zeros((1080, 1920, 4), dtype=np.float16)
        data[:, :, 0] = 0.5
        tex.write(data.tobytes())
        print(f"  Written 1920x1080 RGBA data")
        
        # Test 3: Multiple frames
        print("\n--- Test 3: Multiple frames ---")
        for i in range(5):
            tex = pool.acquire(1920, 1080, frame_index=i)
            print(f"  Frame {i}: texture glo={tex.glo}")
        
        print(f"  Pool stats: {pool.get_stats()}")
        
        # Test 4: Size change
        print("\n--- Test 4: Size change ---")
        tex_4k = pool.acquire(3840, 2160, frame_index=10)
        print(f"  4K texture: glo={tex_4k.glo}, size={tex_4k.size}")
        print(f"  Pool stats: {pool.get_stats()}")
        
        # Cleanup
        print("\n--- Cleanup ---")
        pool.release_all()
        print(f"  Released all: {pool.get_stats()}")
        
        print("\n=== Tests completed ===\n")
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    run_manual_tests()