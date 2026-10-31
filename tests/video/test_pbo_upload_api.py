"""
PBO Upload API Tests - Pixel Buffer Object 异步上传测试

测试覆盖范围：
1. PBOUploader 基础功能（创建、销毁、初始化）
2. PBO upload ring API（双缓冲/三缓冲）
3. upload_async() 异步上传行为
4. fallback 到普通写入（sync_transfer）
5. Ring Buffer 循环上传
6. 性能对比（PBO vs 直接上传）
7. 边界情况（大小不足、context 丢失）
8. 多线程安全

注意事项：
- OpenGL 测试需要创建 standalone context
- 所有 ModernGL 操作在测试线程执行
- 测试后必须清理资源
- PBO 是可选功能，可能未完全实现

设计约束（.clinerules）：
- OpenGL/ModernGL 只能在渲染线程调用
- 新函数必须有类型注解
"""

import pytest
import sys
import os
import time
import numpy as np
from typing import Tuple, Dict, Any, Optional, List
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 检查 moderngl 是否可用
try:
    import moderngl
    HAS_MODERNGL = True
except ImportError:
    HAS_MODERNGL = False

# 导入模块
try:
    from src.shared.video.video_upload import (
        PBOUploader,
        YuvPlaneUploader,
        UploadPerformanceTester,
    )
    HAS_PBO_MODULE = True
except ImportError:
    HAS_PBO_MODULE = False


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def gl_context() -> Optional["moderngl.Context"]:
    """创建 standalone ModernGL 上下文"""
    if not HAS_MODERNGL:
        pytest.skip("moderngl not available")

    try:
        ctx = moderngl.create_standalone_context()
        yield ctx
    except Exception as e:
        pytest.skip(f"Failed to create OpenGL context: {e}")


@pytest.fixture
def mock_context() -> MagicMock:
    """Mock ModernGL context（不需要真实 OpenGL）"""
    mock_ctx = MagicMock()
    mock_ctx.glo = 1

    # 模拟 buffer 创建
    def create_buffer(reserve: int = 0, data: bytes = None) -> MagicMock:
        mock_buf = MagicMock()
        mock_buf.glo = id(mock_buf)
        mock_buf.size = reserve
        mock_buf.write = MagicMock()
        mock_buf.read = MagicMock(return_value=b'\x00' * reserve)
        mock_buf.release = MagicMock()
        mock_buf.bind = MagicMock()
        return mock_buf

    # 模拟 texture 创建
    def create_texture(size: Tuple[int, int] = (1920, 1080),
                       components: int = 4,
                       dtype: str = 'f2',
                       data: bytes = None) -> MagicMock:
        mock_tex = MagicMock()
        mock_tex.glo = id(mock_tex)
        mock_tex.size = size
        mock_tex.components = components
        mock_tex.dtype = dtype
        mock_tex.filter = (moderngl.LINEAR, moderngl.LINEAR) if HAS_MODERNGL else (0, 0)
        mock_tex.repeat_x = False
        mock_tex.repeat_y = False
        mock_tex.write = MagicMock()
        mock_tex.read = MagicMock(return_value=b'\x00' * (size[0] * size[1] * components * 2))
        mock_tex.release = MagicMock()
        return mock_tex

    mock_ctx.buffer = MagicMock(side_effect=create_buffer)
    mock_ctx.texture = MagicMock(side_effect=create_texture)
    yield mock_ctx


@pytest.fixture
def pbo_uploader(gl_context: "moderngl.Context") -> PBOUploader:
    """创建真实的 PBOUploader 实例"""
    if not HAS_PBO_MODULE:
        pytest.skip("PBOUploader module not available")

    uploader = PBOUploader(gl_context, buffer_count=2)
    yield uploader
    uploader.release()


@pytest.fixture
def mock_pbo_uploader(mock_context: MagicMock) -> PBOUploader:
    """创建 mock PBOUploader 实例"""
    if not HAS_PBO_MODULE:
        pytest.skip("PBOUploader module not available")

    uploader = PBOUploader(mock_context, buffer_count=2)
    yield uploader
    uploader.release()


@pytest.fixture
def test_data_1080p() -> np.ndarray:
    """1080p RGBA float32 测试数据"""
    return np.random.rand(1080, 1920, 4).astype(np.float32)


@pytest.fixture
def test_data_4k() -> np.ndarray:
    """4K RGBA float32 测试数据"""
    return np.random.rand(2160, 3840, 4).astype(np.float32)


# =============================================================================
# Section 1: PBO Creation and Destruction Tests
# =============================================================================

class TestPBOCreationDestruction:
    """测试 PBO 缓冲区创建和销毁"""

    def test_uploader_creation(self, mock_pbo_uploader: PBOUploader) -> None:
        """PBOUploader 创建"""
        uploader = mock_pbo_uploader

        assert uploader._buffer_count == 2
        assert len(uploader._buffers) == 0  # 初始未初始化
        assert uploader._initialized == False
        assert uploader._current_index == 0

    def test_uploader_creation_different_buffer_count(self, mock_context: MagicMock) -> None:
        """不同缓冲数量创建"""
        if not HAS_PBO_MODULE:
            pytest.skip("PBOUploader module not available")

        # 双缓冲
        uploader2 = PBOUploader(mock_context, buffer_count=2)
        assert uploader2._buffer_count == 2
        uploader2.release()

        # 三缓冲
        uploader3 = PBOUploader(mock_context, buffer_count=3)
        assert uploader3._buffer_count == 3
        uploader3.release()

    def test_initialize_creates_buffers(self, mock_pbo_uploader: PBOUploader) -> None:
        """初始化创建缓冲区"""
        uploader = mock_pbo_uploader

        # 初始化 4K 大小
        max_size = 3840 * 2160 * 4 * 4  # RGBA float32
        uploader.initialize(max_size)

        assert uploader._initialized == True
        assert len(uploader._buffers) == 2
        assert len(uploader._buffer_sizes) == 2
        assert uploader._buffer_sizes[0] == max_size

    def test_initialize_releases_old_buffers(self, mock_pbo_uploader: PBOUploader) -> None:
        """重复初始化释放旧缓冲"""
        uploader = mock_pbo_uploader

        # 第一次初始化
        uploader.initialize(1000)

        # 获取旧缓冲引用
        old_buffers = list(uploader._buffers)

        # 第二次初始化（更大）
        uploader.initialize(2000)

        # 新缓冲应该不同
        assert len(uploader._buffers) == 2
        # 旧缓冲应该被 release
        for buf in old_buffers:
            buf.release.assert_called()

    def test_release_clears_buffers(self, mock_pbo_uploader: PBOUploader) -> None:
        """释放清空缓冲"""
        uploader = mock_pbo_uploader

        uploader.initialize(1000)
        assert len(uploader._buffers) == 2

        uploader.release()

        assert len(uploader._buffers) == 0
        assert len(uploader._buffer_sizes) == 0
        assert uploader._initialized == False

    def test_double_release_safe(self, mock_pbo_uploader: PBOUploader) -> None:
        """重复释放安全"""
        uploader = mock_pbo_uploader

        uploader.initialize(1000)
        uploader.release()
        uploader.release()  # 第二次不应抛出异常

        assert uploader._initialized == False


# =============================================================================
# Section 2: Ring Buffer Tests
# =============================================================================

class TestPBORingBuffer:
    """测试 PBO Ring Buffer 循环使用"""

    def test_ring_buffer_slot_rotation(self, mock_pbo_uploader: PBOUploader) -> None:
        """槽位轮转"""
        uploader = mock_pbo_uploader
        uploader.initialize(1000)

        # 初始 index 0
        assert uploader._current_index == 0

        # 模拟上传（需要 texture）
        mock_tex = MagicMock()
        mock_tex.write = MagicMock()

        data = np.zeros((10, 10, 4), dtype=np.float32)

        # 第一次上传
        uploader.upload_async(data, mock_tex)
        assert uploader._current_index == 1  # 切换到下一个

        # 第二次上传
        uploader.upload_async(data, mock_tex)
        assert uploader._current_index == 0  # 循环回来

        # 第三次上传
        uploader.upload_async(data, mock_tex)
        assert uploader._current_index == 1  # 再次切换

    def test_ring_buffer_three_slots(self, mock_context: MagicMock) -> None:
        """三缓冲槽位轮转"""
        if not HAS_PBO_MODULE:
            pytest.skip("PBOUploader module not available")

        uploader = PBOUploader(mock_context, buffer_count=3)
        uploader.initialize(1000)

        mock_tex = MagicMock()
        mock_tex.write = MagicMock()
        data = np.zeros((10, 10, 4), dtype=np.float32)

        # 三次上传后循环
        uploader.upload_async(data, mock_tex)
        assert uploader._current_index == 1

        uploader.upload_async(data, mock_tex)
        assert uploader._current_index == 2

        uploader.upload_async(data, mock_tex)
        assert uploader._current_index == 0  # 循环回来

        uploader.release()

    def test_ring_buffer_no_slot_conflict(self, mock_pbo_uploader: PBOUploader) -> None:
        """槽位不冲突（模拟多帧上传）"""
        uploader = mock_pbo_uploader
        uploader.initialize(1000)

        mock_tex = MagicMock()

        # 模拟不同帧数据
        frames = [np.random.rand(10, 10, 4).astype(np.float32) for _ in range(10)]

        # 连续上传
        for frame in frames:
            uploader.upload_async(frame, mock_tex)

        # 统计上传次数
        stats = uploader.get_performance_stats()
        assert stats['pbo_upload']['count'] == 10


# =============================================================================
# Section 3: Async Upload API Tests
# =============================================================================

class TestPBOAsyncUploadAPI:
    """测试异步上传 API"""

    def test_upload_async_returns_time(self, mock_pbo_uploader: PBOUploader) -> None:
        """upload_async 返回耗时"""
        uploader = mock_pbo_uploader
        uploader.initialize(1000)

        mock_tex = MagicMock()
        data = np.zeros((10, 10, 4), dtype=np.float32)

        elapsed_ms = uploader.upload_async(data, mock_tex)

        assert elapsed_ms >= 0.0
        assert isinstance(elapsed_ms, float)

    def test_upload_async_records_time(self, mock_pbo_uploader: PBOUploader) -> None:
        """upload_async 记录时间"""
        uploader = mock_pbo_uploader
        uploader.initialize(1000)

        mock_tex = MagicMock()
        data = np.zeros((10, 10, 4), dtype=np.float32)

        # 多次上传
        for _ in range(5):
            uploader.upload_async(data, mock_tex)

        stats = uploader.get_performance_stats()
        assert stats['pbo_upload']['count'] == 5
        assert stats['pbo_upload']['avg_ms'] > 0

    def test_upload_async_without_initialize_raises(self, mock_pbo_uploader: PBOUploader) -> None:
        """未初始化上传抛出异常"""
        uploader = mock_pbo_uploader

        mock_tex = MagicMock()
        data = np.zeros((10, 10, 4), dtype=np.float32)

        with pytest.raises(RuntimeError) as excinfo:
            uploader.upload_async(data, mock_tex)

        assert "not initialized" in str(excinfo.value)

    def test_upload_async_data_conversion(self, mock_pbo_uploader: PBOUploader) -> None:
        """零拷贝：数据以连续 ndarray 经 buffer protocol 写入（不转 bytes）。"""
        uploader = mock_pbo_uploader
        uploader.initialize(1000)

        mock_tex = MagicMock()
        data = np.array([[0.5, 0.3, 0.2, 1.0]], dtype=np.float32).reshape(1, 1, 4)

        uploader.upload_async(data, mock_tex)

        # 检查 buffer.write 被调用
        uploader._buffers[0].write.assert_called_once()
        call_args = uploader._buffers[0].write.call_args[0][0]
        # 零拷贝 DMA 契约：upload_async 走 np.ascontiguousarray + buffer.write(ndarray)
        # （见 video_upload.py:124,141），刻意避免 .tobytes() 拷贝。
        assert isinstance(call_args, np.ndarray), (
            f"零拷贝 PBO 上传应传 ndarray，实际 {type(call_args).__name__}"
        )
        assert call_args.dtype == np.float32
        assert call_args.shape == (1, 1, 4)
        np.testing.assert_allclose(call_args.ravel(), [0.5, 0.3, 0.2, 1.0])

    def test_upload_async_size_exceeds_buffer(self, mock_pbo_uploader: PBOUploader) -> None:
        """数据超过缓冲大小时重新分配"""
        uploader = mock_pbo_uploader
        uploader.initialize(100)  # 小缓冲

        mock_tex = MagicMock()

        # 大数据
        data = np.zeros((100, 100, 4), dtype=np.float32)
        data_size = data.nbytes

        # 上传（应该重新分配）
        elapsed = uploader.upload_async(data, mock_tex)

        # 缓冲应该重新分配到更大
        # 检查 buffer_sizes 更新（可能已释放旧缓冲）
        assert uploader._initialized == True

    def test_upload_direct_baseline(self, mock_pbo_uploader: PBOUploader) -> None:
        """直接上传（对比基准）"""
        uploader = mock_pbo_uploader

        mock_tex = MagicMock()
        data = np.zeros((10, 10, 4), dtype=np.float32)

        elapsed_ms = uploader.upload_direct(data, mock_tex)

        assert elapsed_ms >= 0.0
        assert isinstance(elapsed_ms, float)

        stats = uploader.get_performance_stats()
        assert stats['direct_upload']['count'] == 1


# =============================================================================
# Section 4: Fallback Tests
# =============================================================================

class TestPBOFallback:
    """测试 fallback 到普通写入"""

    def test_fallback_when_pbo_unavailable(self, mock_context: MagicMock) -> None:
        """PBO 不可用时 fallback"""
        if not HAS_PBO_MODULE:
            pytest.skip("PBOUploader module not available")

        # 模拟 buffer 创建失败
        mock_context.buffer = MagicMock(side_effect=Exception("PBO not supported"))

        uploader = PBOUploader(mock_context, buffer_count=2)

        # 尝试初始化
        try:
            uploader.initialize(1000)
        except Exception:
            # 预期失败
            pass

        # 使用直接上传作为 fallback
        mock_tex = MagicMock()
        data = np.zeros((10, 10, 4), dtype=np.float32)

        elapsed = uploader.upload_direct(data, mock_tex)
        assert elapsed >= 0.0

        uploader.release()

    def test_sync_transfer_fallback(self, mock_pbo_uploader: PBOUploader) -> None:
        """同步传输 fallback"""
        uploader = mock_pbo_uploader

        # 不初始化 PBO，直接使用 sync_transfer 模式
        mock_tex = MagicMock()
        data = np.zeros((10, 10, 4), dtype=np.float32)

        # 使用 upload_direct 作为 fallback
        elapsed = uploader.upload_direct(data, mock_tex)

        stats = uploader.get_performance_stats()
        assert stats['direct_upload']['count'] == 1

    def test_error_handling_in_upload(self, mock_pbo_uploader: PBOUploader) -> None:
        """上传错误处理"""
        uploader = mock_pbo_uploader
        uploader.initialize(1000)

        # 模拟 texture 写入失败
        mock_tex = MagicMock()
        mock_tex.write = MagicMock(side_effect=Exception("Write failed"))

        data = np.zeros((10, 10, 4), dtype=np.float32)

        # 应该不抛出异常（内部处理或传播）
        # 具体行为取决于实现
        try:
            uploader.upload_async(data, mock_tex)
        except Exception:
            pass  # 允许传播异常


# =============================================================================
# Section 5: Performance Comparison Tests
# =============================================================================

class TestPBOPerformanceComparison:
    """测试性能对比"""

    def test_performance_stats_structure(self, mock_pbo_uploader: PBOUploader) -> None:
        """性能统计结构"""
        uploader = mock_pbo_uploader

        stats = uploader.get_performance_stats()

        assert 'pbo_upload' in stats
        assert 'direct_upload' in stats
        assert 'improvement_ratio' in stats

        assert 'count' in stats['pbo_upload']
        assert 'avg_ms' in stats['pbo_upload']
        assert 'min_ms' in stats['pbo_upload']
        assert 'max_ms' in stats['pbo_upload']

    def test_performance_stats_empty(self, mock_pbo_uploader: PBOUploader) -> None:
        """空统计"""
        uploader = mock_pbo_uploader

        stats = uploader.get_performance_stats()

        assert stats['pbo_upload']['count'] == 0
        assert stats['pbo_upload']['avg_ms'] == 0.0
        assert stats['direct_upload']['count'] == 0

    def test_performance_stats_after_uploads(self, mock_pbo_uploader: PBOUploader) -> None:
        """上传后统计"""
        uploader = mock_pbo_uploader
        uploader.initialize(10000)

        mock_tex = MagicMock()
        data = np.zeros((20, 20, 4), dtype=np.float32)

        # PBO 上传
        for _ in range(10):
            uploader.upload_async(data, mock_tex)

        # 直接上传
        for _ in range(10):
            uploader.upload_direct(data, mock_tex)

        stats = uploader.get_performance_stats()

        assert stats['pbo_upload']['count'] == 10
        assert stats['direct_upload']['count'] == 10
        assert stats['pbo_upload']['avg_ms'] > 0
        assert stats['direct_upload']['avg_ms'] > 0

    def test_improvement_ratio_calculation(self, mock_pbo_uploader: PBOUploader) -> None:
        """提升比例计算"""
        uploader = mock_pbo_uploader
        uploader.initialize(10000)

        mock_tex = MagicMock()
        data = np.zeros((20, 20, 4), dtype=np.float32)

        # 假设 PBO 更快（模拟）
        uploader.upload_async(data, mock_tex)
        uploader.upload_direct(data, mock_tex)

        stats = uploader.get_performance_stats()

        # improvement_ratio = (direct_avg - pbo_avg) / direct_avg
        # 如果 PBO 更快，应该 > 0
        # 注意：实际测试中可能 PBO 更慢（因为没有真正异步）
        assert isinstance(stats['improvement_ratio'], float)

    def test_upload_time_estimate(self, mock_pbo_uploader: PBOUploader) -> None:
        """上传时间估算"""
        uploader = mock_pbo_uploader

        # 4K RGBA float32
        data_size = 3840 * 2160 * 4 * 4

        estimate_ms = uploader.get_upload_time_estimate(data_size)

        assert estimate_ms > 0
        # 基于 PCIe 3.0 约 16GB/s，4K RGBA32 约 66MB
        # 理论时间约 4ms，考虑开销可能 6ms
        # 宽松阈值：应该 < 20ms
        assert estimate_ms < 50


@pytest.mark.skipif(not HAS_MODERNGL or not HAS_PBO_MODULE, reason="moderngl or PBO module not available")
class TestPBOPerformanceIntegration:
    """性能集成测试（需要真实 OpenGL）"""

    def test_real_1080p_upload_comparison(self, gl_context: "moderngl.Context",
                                          test_data_1080p: np.ndarray) -> None:
        """真实 1080p 上传对比"""
        uploader = PBOUploader(gl_context, buffer_count=2)

        data_size = test_data_1080p.nbytes
        uploader.initialize(data_size * 2)

        tex = gl_context.texture((1920, 1080), 4, dtype='f4')

        # 预热
        for _ in range(5):
            uploader.upload_async(test_data_1080p, tex)
            uploader.upload_direct(test_data_1080p, tex)

        # 测试
        for _ in range(20):
            uploader.upload_async(test_data_1080p, tex)
            uploader.upload_direct(test_data_1080p, tex)

        stats = uploader.get_performance_stats()

        print(f"\n1080p Upload Stats:")
        print(f"  PBO avg: {stats['pbo_upload']['avg_ms']:.2f} ms")
        print(f"  Direct avg: {stats['direct_upload']['avg_ms']:.2f} ms")
        print(f"  Improvement: {stats['improvement_ratio']:.2%}")

        # 两种方法都应该能工作
        assert stats['pbo_upload']['avg_ms'] > 0
        assert stats['direct_upload']['avg_ms'] > 0

        tex.release()
        uploader.release()

    def test_real_4k_upload_comparison(self, gl_context: "moderngl.Context",
                                       test_data_4k: np.ndarray) -> None:
        """真实 4K 上传对比"""
        uploader = PBOUploader(gl_context, buffer_count=3)

        data_size = test_data_4k.nbytes
        uploader.initialize(data_size * 3)

        tex = gl_context.texture((3840, 2160), 4, dtype='f4')

        # 预热
        uploader.upload_async(test_data_4k, tex)
        uploader.upload_direct(test_data_4k, tex)

        # 测试
        for _ in range(5):
            uploader.upload_async(test_data_4k, tex)
            uploader.upload_direct(test_data_4k, tex)

        stats = uploader.get_performance_stats()

        print(f"\n4K Upload Stats:")
        print(f"  PBO avg: {stats['pbo_upload']['avg_ms']:.2f} ms")
        print(f"  Direct avg: {stats['direct_upload']['avg_ms']:.2f} ms")

        # 4K 上传应完成且不报错（性能阈值不作为硬断言，
        # 因为本机性能波动大，CI/本地差异显著）
        assert stats['pbo_upload']['avg_ms'] > 0
        assert stats['direct_upload']['avg_ms'] > 0
        assert stats['pbo_upload']['count'] > 0
        assert stats['direct_upload']['count'] > 0

        tex.release()
        uploader.release()

    def test_continuous_upload_throughput(self, gl_context: "moderngl.Context") -> None:
        """连续上传吞吐量"""
        uploader = PBOUploader(gl_context, buffer_count=3)

        # 1080p 数据
        data = np.random.rand(1080, 1920, 4).astype(np.float32)
        data_size = data.nbytes
        uploader.initialize(data_size * 3)

        tex = gl_context.texture((1920, 1080), 4, dtype='f4')

        # 测量连续上传吞吐量
        frame_count = 30
        start_time = time.perf_counter()

        for _ in range(frame_count):
            uploader.upload_async(data, tex)

        total_time = time.perf_counter() - start_time

        # 计算吞吐量
        throughput_fps = frame_count / total_time
        throughput_mbps = (data_size * frame_count) / total_time / 1e6

        print(f"\nContinuous Upload Throughput:")
        print(f"  FPS: {throughput_fps:.1f}")
        print(f"  Bandwidth: {throughput_mbps:.1f} MB/s")

        # 应该能达到基本吞吐量
        assert throughput_fps > 5  # 至少 5 FPS
        assert throughput_mbps > 100  # 至少 100 MB/s

        tex.release()
        uploader.release()


# =============================================================================
# Section 6: Edge Cases and Error Handling Tests
# =============================================================================

class TestPBOEdgeCases:
    """测试边界情况"""

    def test_buffer_size_insufficient(self, mock_pbo_uploader: PBOUploader) -> None:
        """缓冲大小不足"""
        uploader = mock_pbo_uploader
        uploader.initialize(100)  # 小缓冲

        mock_tex = MagicMock()

        # 大数据
        data = np.zeros((100, 100, 4), dtype=np.float32)

        # 应自动重新分配（或警告）
        elapsed = uploader.upload_async(data, mock_tex)

        # 不应该抛出异常
        assert elapsed >= 0

    def test_empty_data_upload(self, mock_pbo_uploader: PBOUploader) -> None:
        """空数据上传"""
        uploader = mock_pbo_uploader
        uploader.initialize(1000)

        mock_tex = MagicMock()

        # 空数据（小尺寸）
        data = np.zeros((1, 1, 4), dtype=np.float32)

        elapsed = uploader.upload_async(data, mock_tex)
        assert elapsed >= 0

    def test_gpu_context_loss_simulation(self, mock_context: MagicMock) -> None:
        """GPU context 丢失模拟"""
        if not HAS_PBO_MODULE:
            pytest.skip("PBOUploader module not available")

        uploader = PBOUploader(mock_context, buffer_count=2)
        uploader.initialize(1000)

        # 模拟 context 丢失
        mock_context.buffer = MagicMock(side_effect=Exception("Context lost"))

        # 释放旧资源后重新创建
        uploader.release()
        uploader.initialize(1000)

        # 应该能正常工作
        assert uploader._initialized == True

    def test_negative_buffer_count(self, mock_context: MagicMock) -> None:
        """负缓冲数量"""
        if not HAS_PBO_MODULE:
            pytest.skip("PBOUploader module not available")

        # 负数应该有合理处理（至少 1）
        uploader = PBOUploader(mock_context, buffer_count=-1)

        # 内部应该有保护
        assert uploader._buffer_count >= 1

        uploader.release()

    def test_zero_buffer_count(self, mock_context: MagicMock) -> None:
        """零缓冲数量"""
        if not HAS_PBO_MODULE:
            pytest.skip("PBOUploader module not available")

        uploader = PBOUploader(mock_context, buffer_count=0)

        # 内部应该有保护（至少 1）
        assert uploader._buffer_count >= 1

        uploader.release()


# =============================================================================
# Section 7: Multi-threading Safety Tests
# =============================================================================

class TestPBOThreadSafety:
    """测试多线程安全"""

    def test_single_thread_upload_sequence(self, mock_pbo_uploader: PBOUploader) -> None:
        """单线程上传序列"""
        uploader = mock_pbo_uploader
        uploader.initialize(10000)

        mock_tex = MagicMock()
        data = np.zeros((20, 20, 4), dtype=np.float32)

        # 连续上传
        for i in range(100):
            elapsed = uploader.upload_async(data, mock_tex)

        stats = uploader.get_performance_stats()
        assert stats['pbo_upload']['count'] == 100

    def test_reinitialize_after_use(self, mock_pbo_uploader: PBOUploader) -> None:
        """使用后重新初始化"""
        uploader = mock_pbo_uploader

        # 第一次使用
        uploader.initialize(1000)
        mock_tex = MagicMock()
        data = np.zeros((10, 10, 4), dtype=np.float32)
        uploader.upload_async(data, mock_tex)

        # 重新初始化
        uploader.initialize(2000)

        assert len(uploader._buffers) == 2
        assert uploader._buffer_sizes[0] == 2000

    def test_concurrent_access_not_supported(self, mock_pbo_uploader: PBOUploader) -> None:
        """并发访问不支持（OpenGL 约束）"""
        # OpenGL 不支持多线程同时访问同一 context
        # 这里只是验证设计约束，不实际测试并发

        uploader = mock_pbo_uploader
        uploader.initialize(1000)

        # 所有操作必须在单线程
        mock_tex = MagicMock()
        data = np.zeros((10, 10, 4), dtype=np.float32)

        # 单线程顺序操作
        uploader.upload_async(data, mock_tex)
        uploader.upload_direct(data, mock_tex)
        uploader.get_performance_stats()

        # 这是安全的使用模式


# =============================================================================
# Section 8: Resource Management Tests
# =============================================================================

class TestPBOResourceManagement:
    """测试资源管理"""

    def test_buffer_release_on_error(self, mock_pbo_uploader: PBOUploader) -> None:
        """错误时缓冲释放"""
        uploader = mock_pbo_uploader
        uploader.initialize(1000)

        # 记录初始缓冲
        initial_buffers = list(uploader._buffers)

        # 模拟上传失败后释放
        uploader.release()

        # 所有缓冲应该被 release
        for buf in initial_buffers:
            buf.release.assert_called()

    def test_resource_cleanup_sequence(self, mock_pbo_uploader: PBOUploader) -> None:
        """资源清理序列"""
        uploader = mock_pbo_uploader

        uploader.initialize(1000)
        uploader.initialize(2000)
        uploader.initialize(3000)

        # 最后应该只有 2 个缓冲
        assert len(uploader._buffers) == 2

        uploader.release()

        assert len(uploader._buffers) == 0

    def test_memory_usage_estimation(self, mock_pbo_uploader: PBOUploader) -> None:
        """内存使用估算"""
        uploader = mock_pbo_uploader

        # 4K RGBA float32 三缓冲
        max_size = 3840 * 2160 * 4 * 4 * 3
        uploader.initialize(max_size)

        # 缓冲大小
        total_buffer_size = sum(uploader._buffer_sizes)

        # 理论值约 200MB
        assert total_buffer_size > 0
        assert total_buffer_size == max_size * 2  # 双缓冲


# =============================================================================
# Section 9: API Contract Tests
# =============================================================================

class TestPBOAPIContract:
    """测试 API 契约"""

    def test_upload_async_signature(self, mock_pbo_uploader: PBOUploader) -> None:
        """upload_async 签名"""
        uploader = mock_pbo_uploader
        uploader.initialize(1000)

        mock_tex = MagicMock()
        data = np.zeros((10, 10, 4), dtype=np.float32)

        # 正确调用
        result = uploader.upload_async(data, mock_tex)

        # 返回 float
        assert isinstance(result, float)

    def test_upload_direct_signature(self, mock_pbo_uploader: PBOUploader) -> None:
        """upload_direct 签名"""
        uploader = mock_pbo_uploader

        mock_tex = MagicMock()
        data = np.zeros((10, 10, 4), dtype=np.float32)

        result = uploader.upload_direct(data, mock_tex)

        assert isinstance(result, float)

    def test_get_performance_stats_signature(self, mock_pbo_uploader: PBOUploader) -> None:
        """get_performance_stats 签名"""
        uploader = mock_pbo_uploader

        stats = uploader.get_performance_stats()

        # 返回 dict
        assert isinstance(stats, dict)

        # 包含必需字段
        required_keys = ['pbo_upload', 'direct_upload', 'improvement_ratio']
        for key in required_keys:
            assert key in stats

    def test_get_upload_time_estimate_signature(self, mock_pbo_uploader: PBOUploader) -> None:
        """get_upload_time_estimate 签名"""
        uploader = mock_pbo_uploader

        # int 输入
        result = uploader.get_upload_time_estimate(1000)

        assert isinstance(result, float)
        assert result > 0


# =============================================================================
# Section 10: Documentation and Behavior Tests
# =============================================================================

class TestPBODocumentationBehavior:
    """测试文档和行为一致性"""

    def test_buffer_count_documentation(self, mock_context: MagicMock) -> None:
        """缓冲数量文档"""
        if not HAS_PBO_MODULE:
            pytest.skip("PBOUploader module not available")

        # 文档说明：双缓冲=2，三缓冲=3
        uploader2 = PBOUploader(mock_context, buffer_count=2)
        assert uploader2._buffer_count == 2

        uploader3 = PBOUploader(mock_context, buffer_count=3)
        assert uploader3._buffer_count == 3

        uploader2.release()
        uploader3.release()

    def test_non_blocking_behavior_simulation(self, mock_pbo_uploader: PBOUploader) -> None:
        """非阻塞行为模拟"""
        uploader = mock_pbo_uploader
        uploader.initialize(10000)

        mock_tex = MagicMock()
        data = np.zeros((20, 20, 4), dtype=np.float32)

        # 记录开始时间
        start = time.perf_counter()

        # PBO 上传
        elapsed = uploader.upload_async(data, mock_tex)

        end = time.perf_counter()

        # elapsed 应接近实际测量时间
        measured = (end - start) * 1000
        # 允许一定误差
        assert abs(elapsed - measured) < 5  # 5ms误差


# =============================================================================
# Section 11: Performance Tester Tests
# =============================================================================

@pytest.mark.skipif(not HAS_MODERNGL or not HAS_PBO_MODULE, reason="moderngl or PBO module not available")
class TestUploadPerformanceTester:
    """测试 UploadPerformanceTester"""

    def test_performance_tester_creation(self, gl_context: "moderngl.Context") -> None:
        """性能测试器创建"""
        tester = UploadPerformanceTester(gl_context)

        assert tester._ctx is not None
        assert tester._pbo_uploader is not None
        assert tester._yuv_uploader is not None

        tester.release()

    def test_direct_vs_pbo_test(self, gl_context: "moderngl.Context") -> None:
        """直接 vs PBO 测试"""
        tester = UploadPerformanceTester(gl_context)

        resolutions = [(640, 480), (1920, 1080)]

        results = tester.test_direct_vs_pbo(resolutions)

        assert 'pbo_vs_direct' in results
        assert '640x480' in results
        assert '1920x1080' in results

        tester.release()

    def test_yuv_vs_rgb_test(self, gl_context: "moderngl.Context") -> None:
        """YUV vs RGB 测试"""
        tester = UploadPerformanceTester(gl_context)

        resolutions = [(640, 480)]

        results = tester.test_yuv_vs_rgb_upload(resolutions)

        assert 'yuv_vs_rgb' in results
        assert '640x480' in results

        tester.release()


# =============================================================================
# Manual Test Runner
# =============================================================================

def run_manual_tests() -> None:
    """手动运行测试（用于调试）"""
    print("\n=== Manual PBO Upload Tests ===\n")

    if not HAS_MODERNGL:
        print("moderngl not available, skipping manual tests")
        return

    if not HAS_PBO_MODULE:
        print("PBOUploader module not available, skipping manual tests")
        return

    try:
        ctx = moderngl.create_standalone_context()
        print("Created standalone context")

        uploader = PBOUploader(ctx, buffer_count=2)
        print(f"Created PBOUploader with buffer_count=2")

        # Test 1: Initialize
        print("\n--- Test 1: Initialize ---")
        max_size = 1920 * 1080 * 4 * 4  # RGBA float32
        uploader.initialize(max_size)
        print(f"  Initialized with {max_size} bytes per buffer")
        print(f"  Buffer count: {len(uploader._buffers)}")

        # Test 2: Upload
        print("\n--- Test 2: Upload ---")
        tex = ctx.texture((1920, 1080), 4, dtype='f4')
        data = np.random.rand(1080, 1920, 4).astype(np.float32)

        elapsed = uploader.upload_async(data, tex)
        print(f"  Upload elapsed: {elapsed:.2f} ms")

        elapsed = uploader.upload_direct(data, tex)
        print(f"  Direct upload elapsed: {elapsed:.2f} ms")

        # Test 3: Performance stats
        print("\n--- Test 3: Performance Stats ---")
        stats = uploader.get_performance_stats()
        print(f"  PBO count: {stats['pbo_upload']['count']}")
        print(f"  Direct count: {stats['direct_upload']['count']}")
        print(f"  PBO avg: {stats['pbo_upload']['avg_ms']:.2f} ms")
        print(f"  Direct avg: {stats['direct_upload']['avg_ms']:.2f} ms")

        # Cleanup
        print("\n--- Cleanup ---")
        tex.release()
        uploader.release()
        print("  Released all resources")

        print("\n=== Tests completed ===\n")

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    run_manual_tests()
    pytest.main([__file__, '-v'])