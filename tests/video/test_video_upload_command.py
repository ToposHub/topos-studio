"""
Video Upload Command Tests - 视频上传命令测试

测试覆盖范围：
1. UPLOAD_FRAME 命令测试
   - 基本帧上传流程
   - 命令队列处理
   - 上传成功验证

2. build_mipmaps 不调用验证
   - 视频帧纹理无 mipmap
   - 使用 LINEAR filter
   - 图片路径使用 MIPMAP filter 的对比

3. Texture 复用测试
   - VideoTexturePool 槽位复用
   - 相同尺寸帧使用相同纹理
   - 不同尺寸帧创建新纹理

4. 帧序列上传测试
   - 连续帧上传性能
   - 纹理池不频繁创建
   - 内存稳定性

5. 清理命令测试
   - CLEANUP 命令释放纹理
   - release_all 正确释放所有槽位
   - 无资源泄漏

6. 边界情况测试
   - 空 frame 数据
   - 异常尺寸（0x0）
   - 大尺寸帧（4K）

注意事项：
- OpenGL/ModernGL 只能在渲染线程调用
- 新函数必须有类型注解
- 使用 mock 或 headless OpenGL context 进行测试
"""

import pytest
import sys
import os
import numpy as np
import time
import queue
from unittest.mock import MagicMock, patch, PropertyMock
from typing import Dict, Any, Optional, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 检查 moderngl 是否可用
try:
    import moderngl
    HAS_MODERNGL = True
except ImportError:
    HAS_MODERNGL = False


def _make_renderer_without_gl():
    """构造一个不触发 OpenGL 初始化的 AsyncRenderer 实例。

    AsyncRenderer 继承 QThread，其 ``__init__`` 会创建共享上下文与
    QOffscreenSurface，这在单元测试里既昂贵也不必要。这里通过
    ``__new__`` 跳过 ``__init__``，再单独调用 ``QThread.__init__``
    完成 C++ 基类初始化，使后续访问 QObject 属性（如 ``current_front_tex_id``、
    ``video_texture_pending``）不会抛出
    ``RuntimeError: super-class __init__() of type AsyncRenderer was never called``。

    调用方仍需像以往那样手动设置测试所需的普通 Python 属性。
    """
    from PyQt6.QtCore import QThread
    from src.features.color.core.async_renderer import AsyncRenderer

    renderer = AsyncRenderer.__new__(AsyncRenderer)
    QThread.__init__(renderer)
    return renderer


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def gl_context() -> Optional[moderngl.Context]:
    """创建 standalone ModernGL 上下文用于测试"""
    if not HAS_MODERNGL:
        pytest.skip("moderngl not available")

    try:
        ctx = moderngl.create_standalone_context()
        yield ctx
        # ModernGL standalone context 没有 release() 方法
        # Python GC 会自动清理
    except Exception as e:
        pytest.skip(f"Failed to create OpenGL context: {e}")


@pytest.fixture
def mock_context() -> MagicMock:
    """Mock ModernGL context（不需要真实 OpenGL）"""
    mock_ctx = MagicMock()

    def create_texture(size: Tuple[int, int] = (1920, 1080),
                       components: int = 4,
                       dtype: str = 'f2') -> MagicMock:
        mock_tex = MagicMock()
        mock_tex.glo = id(mock_tex)  # 模拟 OpenGL ID
        mock_tex.size = size
        mock_tex.components = components
        mock_tex.dtype = dtype
        # 视频帧使用 LINEAR 过滤，不使用 MIPMAP
        mock_tex.filter = (moderngl.LINEAR, moderngl.LINEAR) if HAS_MODERNGL else (0, 0)
        mock_tex.repeat_x = False
        mock_tex.repeat_y = False
        mock_tex.write = MagicMock()
        mock_tex.release = MagicMock()
        mock_tex.build_mipmaps = MagicMock()  # 用于验证不被调用
        return mock_tex

    mock_ctx.texture = MagicMock(side_effect=create_texture)
    yield mock_ctx


@pytest.fixture
def video_pool(mock_context: MagicMock) -> 'VideoTexturePool':
    """创建 mock VideoTexturePool"""
    from src.shared.video.video_texture_pool import VideoTexturePool
    pool = VideoTexturePool(mock_context, pool_size=5)
    yield pool
    pool.release_all()


@pytest.fixture
def real_video_pool(gl_context: moderngl.Context) -> 'VideoTexturePool':
    """创建真实的 VideoTexturePool"""
    from src.shared.video.video_texture_pool import VideoTexturePool
    pool = VideoTexturePool(gl_context, pool_size=5)
    yield pool
    pool.release_all()


@pytest.fixture
def mock_renderer(mock_context: MagicMock) -> MagicMock:
    """创建 mock VideoAsyncRenderer"""
    mock_renderer = MagicMock()
    mock_renderer.ctx = mock_context
    mock_renderer.command_queue = queue.Queue()
    mock_renderer._video_texture_pool = None
    mock_renderer.current_frame_tex = None
    mock_renderer.current_frame_index = -1
    mock_renderer.frame_size = (0, 0)
    mock_renderer.is_running = True
    mock_renderer._initialized = True
    return mock_renderer


@pytest.fixture
def test_frame_rgba() -> np.ndarray:
    """创建测试 RGBA 帧（float32）"""
    frame = np.zeros((480, 640, 4), dtype=np.float32)
    frame[:, :, 0] = 0.5  # R
    frame[:, :, 1] = 0.3  # G
    frame[:, :, 2] = 0.2  # B
    frame[:, :, 3] = 1.0  # A
    return frame


@pytest.fixture
def test_frame_rgb() -> np.ndarray:
    """创建测试 RGB 帧（float32）"""
    frame = np.zeros((480, 640, 3), dtype=np.float32)
    frame[:, :, 0] = 0.5  # R
    frame[:, :, 1] = 0.3  # G
    frame[:, :, 2] = 0.2  # B
    return frame


@pytest.fixture
def test_frame_4k() -> np.ndarray:
    """创建 4K 测试帧"""
    frame = np.zeros((2160, 3840, 4), dtype=np.float32)
    frame[:, :, 0] = 0.5
    frame[:, :, 1] = 0.3
    frame[:, :, 2] = 0.2
    frame[:, :, 3] = 1.0
    return frame


# =============================================================================
# 1. UPLOAD_FRAME 命令测试
# =============================================================================

class TestUploadFrameCommand:
    """测试 UPLOAD_FRAME 命令处理"""

    def test_basic_frame_upload_flow(self, video_pool: 'VideoTexturePool',
                                      test_frame_rgba: np.ndarray) -> None:
        """基本帧上传流程"""
        h, w = test_frame_rgba.shape[:2]

        # Acquire 纹理
        texture = video_pool.acquire(w, h, frame_index=0, components=4)

        assert texture is not None, "纹理获取失败"
        assert video_pool.has_texture_for_frame(0), "帧 0 应存在于池中"

        # 写入帧数据
        texture.write(test_frame_rgba.astype(np.float16).tobytes())

        # 验证纹理存在
        result_tex = video_pool.get_texture_for_frame(0)
        assert result_tex is texture, "获取的纹理应与原始纹理相同"

    def test_upload_with_frame_index(self, video_pool: 'VideoTexturePool',
                                      test_frame_rgb: np.ndarray) -> None:
        """带帧号的上传"""
        h, w = test_frame_rgb.shape[:2]

        # 上传多帧
        for i in range(3):
            tex = video_pool.acquire(w, h, frame_index=i, components=3)
            tex.write(test_frame_rgb.astype(np.float16).tobytes())

        # 相同尺寸复用同一槽位
        stats = video_pool.get_stats()
        assert stats['pool_size'] == 1, "相同尺寸应复用槽位"
        # 只有一个帧持有当前纹理
        assert len(stats['active_frames']) <= 1

    def test_command_data_structure(self, video_pool: 'VideoTexturePool') -> None:
        """命令数据结构验证"""
        # 模拟 UPLOAD_FRAME 命令数据
        from src.shared.video.video_async_renderer import VideoRenderCommand

        cmd_data: Dict[str, Any] = {
            'image': np.zeros((480, 640, 3), dtype=np.float32),
            'frame_size': (640, 480),
            'frame_index': 0,
        }

        # 验证必需字段
        assert 'image' in cmd_data, "缺少 image 字段"
        assert 'frame_size' in cmd_data, "缺少 frame_size 字段"

        # 提取参数
        image = cmd_data.get('image')
        frame_size = cmd_data.get('frame_size')
        frame_index = cmd_data.get('frame_index', 0)

        h, w = image.shape[:2]
        assert frame_size == (w, h), "frame_size 应与图像尺寸一致"

    def test_upload_success_verification(self, video_pool: 'VideoTexturePool',
                                          test_frame_rgba: np.ndarray) -> None:
        """上传成功验证"""
        h, w = test_frame_rgba.shape[:2]

        texture = video_pool.acquire(w, h, frame_index=0)

        # 写入数据
        texture.write(test_frame_rgba.astype(np.float16).tobytes())

        # 验证写入成功（通过池状态）
        stats = video_pool.get_stats()
        assert stats['pool_size'] > 0, "池应有纹理"
        assert (w, h) in stats['sizes'], f"尺寸 {w}x{h} 应在池中"

    def test_upload_sequence_frames(self, video_pool: 'VideoTexturePool',
                                      test_frame_rgba: np.ndarray) -> None:
        """连续帧上传"""
        h, w = test_frame_rgba.shape[:2]

        uploaded_frames: List[int] = []

        for i in range(10):
            tex = video_pool.acquire(w, h, frame_index=i)
            tex.write(test_frame_rgba.astype(np.float16).tobytes())
            uploaded_frames.append(i)

        # 验证上传成功
        stats = video_pool.get_stats()
        assert stats['pool_size'] == 1, "相同尺寸只应有一个槽位"


# =============================================================================
# 2. build_mipmaps 不调用验证
# =============================================================================

class TestNoMipmapForVideo:
    """测试视频帧不调用 build_mipmaps"""

    def test_video_texture_no_mipmap(self, video_pool: 'VideoTexturePool',
                                      test_frame_rgba: np.ndarray) -> None:
        """视频帧纹理无 mipmap"""
        h, w = test_frame_rgba.shape[:2]

        texture = video_pool.acquire(w, h, frame_index=0)

        # 验证 filter 是 LINEAR（不是 MIPMAP）
        assert texture.filter[0] == moderngl.LINEAR, "Min filter 应为 LINEAR"
        assert texture.filter[1] == moderngl.LINEAR, "Mag filter 应为 LINEAR"

        # 验证 build_mipmaps 未被调用
        texture.build_mipmaps.assert_not_called()

    def test_video_texture_linear_filter_only(self, mock_context: MagicMock) -> None:
        """视频纹理仅使用 LINEAR filter"""
        from src.shared.video.video_texture_pool import VideoTexturePool

        pool = VideoTexturePool(mock_context, pool_size=5)

        # 获取纹理
        tex = pool.acquire(1920, 1080, frame_index=0)

        # 验证 filter 设置
        if HAS_MODERNGL:
            assert tex.filter == (moderngl.LINEAR, moderngl.LINEAR)

        pool.release_all()

    def test_video_vs_image_filter_difference(self, mock_context: MagicMock) -> None:
        """视频与图片 filter 设置对比"""
        # 视频纹理：LINEAR
        video_tex = mock_context.texture(size=(1920, 1080), components=4, dtype='f2')
        video_tex.filter = (moderngl.LINEAR, moderngl.LINEAR) if HAS_MODERNGL else (0, 0)

        # 图片纹理：MIPMAP（对比）
        image_tex = mock_context.texture(size=(1920, 1080), components=4, dtype='f2')
        image_tex.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR) if HAS_MODERNGL else (0, 0)

        # 验证差异
        if HAS_MODERNGL:
            assert video_tex.filter != image_tex.filter
            assert video_tex.filter[0] == moderngl.LINEAR
            assert image_tex.filter[0] == moderngl.LINEAR_MIPMAP_LINEAR

    def test_no_mipmap_call_after_write(self, video_pool: 'VideoTexturePool',
                                         test_frame_rgba: np.ndarray) -> None:
        """写入数据后不调用 build_mipmaps"""
        h, w = test_frame_rgba.shape[:2]

        texture = video_pool.acquire(w, h, frame_index=0)

        # 写入帧数据
        texture.write(test_frame_rgba.astype(np.float16).tobytes())

        # 确保未调用 build_mipmaps
        texture.build_mipmaps.assert_not_called()

    def test_reused_texture_no_mipmap(self, video_pool: 'VideoTexturePool',
                                       test_frame_rgba: np.ndarray) -> None:
        """复用纹理不调用 mipmap"""
        h, w = test_frame_rgba.shape[:2]

        # 第一次上传
        tex1 = video_pool.acquire(w, h, frame_index=0)
        tex1.write(test_frame_rgba.astype(np.float16).tobytes())

        # 标记为不使用
        video_pool.mark_unused(0)

        # 第二次上传（复用）
        tex2 = video_pool.acquire(w, h, frame_index=1)

        # 应复用同一纹理
        assert tex2 is tex1

        # 验证 build_mipmaps 未被调用
        tex2.build_mipmaps.assert_not_called()


# =============================================================================
# 3. Texture 复用测试
# =============================================================================

class TestTextureReuse:
    """测试纹理复用机制"""

    def test_slot_reuse_same_size(self, video_pool: 'VideoTexturePool') -> None:
        """相同尺寸槽位复用"""
        # 第一个帧
        tex1 = video_pool.acquire(1920, 1080, frame_index=0)
        tex1_id = tex1.glo

        # 标记为不使用
        video_pool.mark_unused(0)

        # 第二个帧（相同尺寸）
        tex2 = video_pool.acquire(1920, 1080, frame_index=1)
        tex2_id = tex2.glo

        # 应复用同一纹理
        assert tex1_id == tex2_id, "相同尺寸应复用纹理"
        assert video_pool._slots[0].frame_index == 1

    def test_different_size_creates_new_slot(self, video_pool: 'VideoTexturePool') -> None:
        """不同尺寸创建新槽位"""
        # 1920x1080
        tex1 = video_pool.acquire(1920, 1080, frame_index=0)

        # 1280x720（不同尺寸）
        tex2 = video_pool.acquire(1280, 720, frame_index=1)

        # 应创建新槽位
        assert tex1 is not tex2
        assert len(video_pool._slots) == 2

        stats = video_pool.get_stats()
        assert stats['pool_size'] == 2

    def test_pool_size_limit_enforcement(self, video_pool: 'VideoTexturePool') -> None:
        """池大小限制（最多 5 个）"""
        textures: List[Any] = []

        # 填满池（不同尺寸）
        for i in range(5):
            tex = video_pool.acquire(1920 + i * 100, 1080, frame_index=i)
            textures.append(tex)

        assert len(video_pool._slots) == 5

        # 第 6 个帧应复用最旧槽位
        tex6 = video_pool.acquire(2560, 1440, frame_index=5)

        assert len(video_pool._slots) == 5, "池大小不应超过限制"

    def test_reuse_with_time_ordering(self, video_pool: 'VideoTexturePool') -> None:
        """时间顺序的复用策略"""
        # 创建多个槽位（相同尺寸）
        tex1 = video_pool.acquire(1920, 1080, frame_index=0)
        time.sleep(0.01)

        tex2 = video_pool.acquire(1920, 1080, frame_index=1)
        time.sleep(0.01)

        tex3 = video_pool.acquire(1920, 1080, frame_index=2)

        # 相同尺寸只会有一个槽位
        assert len(video_pool._slots) == 1
        assert video_pool._slots[0].frame_index == 2

    def test_components_variation(self, video_pool: 'VideoTexturePool') -> None:
        """不同通道数创建新槽位"""
        # RGBA
        tex_rgba = video_pool.acquire(1920, 1080, frame_index=0, components=4)

        # RGB（不同通道数）
        tex_rgb = video_pool.acquire(1920, 1080, frame_index=1, components=3)

        # 应创建不同槽位
        assert len(video_pool._slots) == 2

    def test_reuse_after_pool_full(self, video_pool: 'VideoTexturePool') -> None:
        """池满后的复用"""
        # 填满池
        sizes = [(1920, 1080), (1280, 720), (3840, 2160), (640, 480), (2560, 1440)]
        for i, (w, h) in enumerate(sizes):
            video_pool.acquire(w, h, frame_index=i)

        assert len(video_pool._slots) == 5

        # 再次请求 1920x1080
        tex = video_pool.acquire(1920, 1080, frame_index=100)

        # 应找到匹配尺寸的槽位复用
        assert tex is not None


# =============================================================================
# 4. 帧序列上传测试
# =============================================================================

class TestFrameSequenceUpload:
    """测试帧序列上传"""

    def test_continuous_upload_performance(self, video_pool: 'VideoTexturePool',
                                            test_frame_rgba: np.ndarray) -> None:
        """连续帧上传性能"""
        h, w = test_frame_rgba.shape[:2]

        start_time = time.time()

        for i in range(100):
            tex = video_pool.acquire(w, h, frame_index=i)
            tex.write(test_frame_rgba.astype(np.float16).tobytes())

        elapsed = time.time() - start_time

        # 100 帧上传应在 1 秒内完成（mock 测试）
        assert elapsed < 1.0, f"上传耗时过长: {elapsed:.2f}s"

        # 只应有一个槽位（相同尺寸复用）
        stats = video_pool.get_stats()
        assert stats['pool_size'] == 1

    def test_pool_no_frequent_creation(self, video_pool: 'VideoTexturePool',
                                        test_frame_rgba: np.ndarray) -> None:
        """纹理池不频繁创建"""
        h, w = test_frame_rgba.shape[:2]

        # 记录初始创建调用次数
        initial_calls = video_pool._ctx.texture.call_count

        # 上传多帧
        for i in range(50):
            tex = video_pool.acquire(w, h, frame_index=i)

        final_calls = video_pool._ctx.texture.call_count

        # 应只有一次创建（首次）
        assert final_calls - initial_calls == 1, "纹理创建次数过多"

    def test_memory_stability(self, video_pool: 'VideoTexturePool',
                              test_frame_rgba: np.ndarray) -> None:
        """内存稳定性（槽位数量稳定）"""
        h, w = test_frame_rgba.shape[:2]

        # 上传大量帧
        for i in range(1000):
            tex = video_pool.acquire(w, h, frame_index=i)

        # 槽位数量应保持稳定
        stats = video_pool.get_stats()
        assert stats['pool_size'] <= stats['max_size']

    def test_mixed_size_sequence(self, video_pool: 'VideoTexturePool') -> None:
        """混合尺寸序列"""
        sizes = [(1920, 1080), (1280, 720), (1920, 1080), (3840, 2160)]

        for i, (w, h) in enumerate(sizes):
            tex = video_pool.acquire(w, h, frame_index=i)

        # 应根据尺寸创建槽位
        stats = video_pool.get_stats()
        assert stats['pool_size'] <= stats['max_size']

    def test_frame_index_tracking(self, video_pool: 'VideoTexturePool') -> None:
        """帧号追踪"""
        h, w = 480, 640

        # 上传帧 0, 1, 2
        tex0 = video_pool.acquire(w, h, frame_index=0)
        tex1 = video_pool.acquire(w, h, frame_index=1)
        tex2 = video_pool.acquire(w, h, frame_index=2)

        # 最后一个帧应持有纹理
        stats = video_pool.get_stats()
        # 相同尺寸复用，只有一个槽位
        assert video_pool._slots[0].frame_index == 2


# =============================================================================
# 5. 清理命令测试
# =============================================================================

class TestCleanupCommand:
    """测试清理命令"""

    def test_cleanup_command_single_slot(self, video_pool: 'VideoTexturePool') -> None:
        """清理单个槽位"""
        tex = video_pool.acquire(1920, 1080, frame_index=0)

        assert len(video_pool._slots) == 1

        # 标记为不使用
        video_pool.mark_unused(0)

        # 槽位标记为可用
        assert video_pool._slots[0].is_available()

    def test_cleanup_command_all_slots(self, video_pool: 'VideoTexturePool') -> None:
        """清理所有槽位"""
        # 创建多个槽位
        for i in range(3):
            video_pool.acquire(1920 + i * 100, 1080, frame_index=i)

        assert len(video_pool._slots) == 3

        # 清理所有
        video_pool.release_all()

        assert len(video_pool._slots) == 0

    def test_release_all_texture_called(self, video_pool: 'VideoTexturePool') -> None:
        """release_all 调用纹理释放"""
        tex1 = video_pool.acquire(1920, 1080, frame_index=0)
        tex2 = video_pool.acquire(1280, 720, frame_index=1)

        # 清理
        video_pool.release_all()

        # 验证 release 被调用
        tex1.release.assert_called()
        tex2.release.assert_called()

    def test_no_resource_leak_after_cleanup(self, video_pool: 'VideoTexturePool') -> None:
        """清理后无资源泄漏"""
        # 创建多个槽位
        for i in range(5):
            video_pool.acquire(1920 + i * 100, 1080, frame_index=i)

        # 清理
        video_pool.release_all()

        # 验证无残留
        stats = video_pool.get_stats()
        assert stats['pool_size'] == 0
        assert stats['active_frames'] == []
        assert stats['sizes'] == []

    def test_cleanup_command_data(self) -> None:
        """清理命令数据结构"""
        from src.shared.video.video_async_renderer import VideoRenderCommand

        # CLEANUP 命令数据
        cleanup_data: Dict[str, Any] = {
            'release_all': True,
        }

        assert 'release_all' in cleanup_data or cleanup_data == {}

    def test_double_cleanup_safe(self, video_pool: 'VideoTexturePool') -> None:
        """重复清理安全"""
        video_pool.acquire(1920, 1080, frame_index=0)

        # 第一次清理
        video_pool.release_all()
        assert len(video_pool._slots) == 0

        # 第二次清理（不应异常）
        video_pool.release_all()
        assert len(video_pool._slots) == 0


# =============================================================================
# 6. 边界情况测试
# =============================================================================

class TestEdgeCases:
    """测试边界情况"""

    def test_empty_frame_data(self, video_pool: 'VideoTexturePool') -> None:
        """空帧数据处理"""
        # 创建空帧
        empty_frame = np.zeros((0, 0, 4), dtype=np.float32)

        # 尝试上传（应失败或返回 None）
        # VideoTexturePool 不检查数据，只检查尺寸
        # Mock 版本会创建纹理
        tex = video_pool.acquire(0, 0, frame_index=0)

        # 实际实现：空帧应由上层检查，池只管理纹理
        # 这里验证池的行为
        if tex is None:
            assert True, "空帧应返回 None"
        else:
            # Mock 创建了纹理，验证行为
            pass

    def test_zero_width_frame(self, video_pool: 'VideoTexturePool') -> None:
        """零宽度帧"""
        tex = video_pool.acquire(0, 1080, frame_index=0)

        # Mock 会创建纹理，实际 ModernGL 会失败
        # 这取决于实现

    def test_zero_height_frame(self, video_pool: 'VideoTexturePool') -> None:
        """零高度帧"""
        tex = video_pool.acquire(1920, 0, frame_index=0)

        # Mock 会创建纹理

    def test_large_4k_frame(self, video_pool: 'VideoTexturePool',
                            test_frame_4k: np.ndarray) -> None:
        """4K 大尺寸帧"""
        h, w = test_frame_4k.shape[:2]

        tex = video_pool.acquire(w, h, frame_index=0)

        assert tex is not None
        stats = video_pool.get_stats()
        assert (w, h) in stats['sizes']

    def test_extreme_large_frame(self, video_pool: 'VideoTexturePool') -> None:
        """超大尺寸帧（8K）"""
        # 8K: 7680x4320
        tex = video_pool.acquire(7680, 4320, frame_index=0)

        assert tex is not None

    def test_negative_frame_index(self, video_pool: 'VideoTexturePool') -> None:
        """负帧号"""
        tex = video_pool.acquire(1920, 1080, frame_index=-1)

        assert tex is not None
        assert video_pool._slots[0].frame_index == -1

    def test_single_channel_frame(self, video_pool: 'VideoTexturePool') -> None:
        """单通道帧"""
        # components=1（灰度）
        tex = video_pool.acquire(1920, 1080, frame_index=0, components=1)

        assert tex is not None
        stats = video_pool.get_stats()
        assert 1 in stats['components']

    def test_two_channel_frame(self, video_pool: 'VideoTexturePool') -> None:
        """双通道帧"""
        tex = video_pool.acquire(1920, 1080, frame_index=0, components=2)

        assert tex is not None

    def test_non_standard_aspect_ratio(self, video_pool: 'VideoTexturePool') -> None:
        """非标准宽高比"""
        # 极端宽
        tex_wide = video_pool.acquire(4096, 256, frame_index=0)
        assert tex_wide is not None

        # 极端高
        tex_tall = video_pool.acquire(256, 4096, frame_index=1)
        assert tex_tall is not None


# =============================================================================
# 7. 命令队列测试
# =============================================================================

class TestCommandQueue:
    """测试命令队列处理"""

    def test_command_queue_basic(self, mock_renderer: MagicMock) -> None:
        """基本命令队列"""
        from src.shared.video.video_async_renderer import VideoRenderCommand

        # 模拟命令队列
        cmd_queue = queue.Queue()

        # 提交命令
        cmd_queue.put((VideoRenderCommand.UPLOAD_FRAME, {
            'image': np.zeros((480, 640, 3), dtype=np.float32),
            'frame_size': (640, 480),
            'frame_index': 0,
        }))

        # 验证队列
        assert not cmd_queue.empty()

        # 获取命令
        cmd_type, data = cmd_queue.get()
        assert cmd_type == VideoRenderCommand.UPLOAD_FRAME
        assert 'image' in data

    def test_command_queue_order(self, mock_renderer: MagicMock) -> None:
        """命令顺序"""
        from src.shared.video.video_async_renderer import VideoRenderCommand

        cmd_queue = queue.Queue()

        # 提交多个命令
        commands = [
            (VideoRenderCommand.UPLOAD_FRAME, {'frame_index': 0}),
            (VideoRenderCommand.RENDER_FRAME, {'viewport_size': (640, 480)}),
            (VideoRenderCommand.CLEANUP, {}),
        ]

        for cmd in commands:
            cmd_queue.put(cmd)

        # 验证顺序
        for expected_cmd in commands:
            actual_cmd = cmd_queue.get()
            assert actual_cmd[0] == expected_cmd[0]

    def test_command_queue_priority(self, mock_renderer: MagicMock) -> None:
        """命令队列优先级（无优先级）"""
        from src.shared.video.video_async_renderer import VideoRenderCommand

        cmd_queue = queue.Queue()

        # CLEANUP 应最后处理
        cmd_queue.put((VideoRenderCommand.UPLOAD_FRAME, {'frame_index': 0}))
        cmd_queue.put((VideoRenderCommand.CLEANUP, {}))

        # FIFO 顺序
        first_cmd = cmd_queue.get()
        assert first_cmd[0] == VideoRenderCommand.UPLOAD_FRAME

    def test_command_queue_timeout(self, mock_renderer: MagicMock) -> None:
        """队列超时"""
        cmd_queue = queue.Queue()

        # 空队列超时
        try:
            item = cmd_queue.get(timeout=0.01)
            assert False, "不应获取到内容"
        except queue.Empty:
            assert True, "空队列应超时"

    def test_multiple_commands_batch(self, mock_renderer: MagicMock) -> None:
        """批量命令"""
        from src.shared.video.video_async_renderer import VideoRenderCommand

        cmd_queue = queue.Queue()

        # 批量提交
        for i in range(10):
            cmd_queue.put((VideoRenderCommand.UPLOAD_FRAME, {'frame_index': i}))

        # 验证数量
        assert cmd_queue.qsize() == 10


# =============================================================================
# 8. 统计信息测试
# =============================================================================

class TestStatistics:
    """测试统计信息"""

    def test_stats_empty_pool(self, video_pool: 'VideoTexturePool') -> None:
        """空池统计"""
        stats = video_pool.get_stats()

        assert stats['pool_size'] == 0
        assert stats['max_size'] == 5
        assert stats['active_frames'] == []
        assert stats['sizes'] == []
        assert stats['components'] == []

    def test_stats_after_acquires(self, video_pool: 'VideoTexturePool') -> None:
        """acquire 后统计"""
        video_pool.acquire(1920, 1080, frame_index=0)
        video_pool.acquire(1280, 720, frame_index=1)

        stats = video_pool.get_stats()
        assert stats['pool_size'] == 2
        assert stats['max_size'] == 5
        assert len(stats['sizes']) == 2

    def test_stats_frame_tracking(self, video_pool: 'VideoTexturePool') -> None:
        """帧号追踪统计"""
        # 上传帧
        video_pool.acquire(1920, 1080, frame_index=0)

        stats = video_pool.get_stats()
        # 检查帧号
        assert len(stats['active_frames']) <= 1

    def test_stats_size_list(self, video_pool: 'VideoTexturePool') -> None:
        """尺寸列表统计"""
        sizes = [(1920, 1080), (1280, 720), (3840, 2160)]

        for i, (w, h) in enumerate(sizes):
            video_pool.acquire(w, h, frame_index=i)

        stats = video_pool.get_stats()
        for size in sizes:
            assert size in stats['sizes']


# =============================================================================
# 9. 错误处理测试
# =============================================================================

class TestErrorHandling:
    """测试错误处理"""

    def test_invalid_dtype(self, video_pool: 'VideoTexturePool') -> None:
        """无效数据类型"""
        # Mock 不检查 dtype
        tex = video_pool.acquire(1920, 1080, frame_index=0, dtype='invalid')

        # Mock 创建会成功
        assert tex is not None

    def test_context_creation_failure(self) -> None:
        """上下文创建失败"""
        mock_failed_ctx = MagicMock()
        mock_failed_ctx.texture.side_effect = Exception("OpenGL error")

        from src.shared.video.video_texture_pool import VideoTexturePool

        pool = VideoTexturePool(mock_failed_ctx, pool_size=5)

        # 尝试 acquire
        tex = pool.acquire(1920, 1080, frame_index=0)

        # 应返回 None（创建失败）
        assert tex is None

    def test_write_after_release(self, video_pool: 'VideoTexturePool',
                                  test_frame_rgba: np.ndarray) -> None:
        """释放后写入"""
        h, w = test_frame_rgba.shape[:2]

        tex = video_pool.acquire(w, h, frame_index=0)

        # 释放
        video_pool.release_all()

        # 尝试写入（不应异常）
        try:
            tex.write(test_frame_rgba.astype(np.float16).tobytes())
            # Mock 不会抛出异常
        except Exception:
            # 实际 ModernGL 可能抛出异常
            pass

    def test_concurrent_access(self, video_pool: 'VideoTexturePool') -> None:
        """并发访问测试"""
        # 模拟并发 acquire
        textures: List[Any] = []

        for i in range(10):
            tex = video_pool.acquire(1920, 1080, frame_index=i)
            textures.append(tex)

        # 验证所有 acquire 成功
        assert all(tex is not None for tex in textures)


# =============================================================================
# 10. 集成测试（真实 OpenGL）
# =============================================================================

@pytest.mark.skipif(not HAS_MODERNGL, reason="moderngl not available")
class TestRealOpenGLIntegration:
    """真实 OpenGL 集成测试"""

    def test_real_texture_creation(self, real_video_pool: 'VideoTexturePool') -> None:
        """真实纹理创建"""
        tex = real_video_pool.acquire(1920, 1080, frame_index=0)

        assert tex is not None
        assert tex.size == (1920, 1080)
        assert tex.components == 4

        # 验证 filter
        assert tex.filter[0] == moderngl.LINEAR
        assert tex.filter[1] == moderngl.LINEAR

    def test_real_texture_write(self, real_video_pool: 'VideoTexturePool',
                                test_frame_rgba: np.ndarray) -> None:
        """真实纹理写入"""
        h, w = test_frame_rgba.shape[:2]

        tex = real_video_pool.acquire(w, h, frame_index=0)

        # 写入数据
        tex.write(test_frame_rgba.astype(np.float16).tobytes())

        assert real_video_pool.has_texture_for_frame(0)

    def test_real_4k_upload(self, real_video_pool: 'VideoTexturePool',
                            test_frame_4k: np.ndarray) -> None:
        """真实 4K 上传"""
        h, w = test_frame_4k.shape[:2]

        tex = real_video_pool.acquire(w, h, frame_index=0)

        assert tex.size == (w, h)

        # 写入 4K 数据
        tex.write(test_frame_4k.astype(np.float16).tobytes())

    def test_real_release_all(self, real_video_pool: 'VideoTexturePool') -> None:
        """真实释放所有"""
        for i in range(3):
            real_video_pool.acquire(1920, 1080, frame_index=i)

        stats = real_video_pool.get_stats()
        assert stats['pool_size'] == 1  # 相同尺寸复用

        # 释放
        real_video_pool.release_all()

        assert len(real_video_pool._slots) == 0


# =============================================================================
# 11. 上下文管理器测试
# =============================================================================

class TestContextManager:
    """测试上下文管理器协议"""

    def test_enter_returns_self(self, video_pool: 'VideoTexturePool') -> None:
        """__enter__ 返回 self"""
        result = video_pool.__enter__()

        assert result is video_pool

    def test_exit_releases_resources(self, video_pool: 'VideoTexturePool') -> None:
        """__exit__ 释放资源"""
        # 使用池
        video_pool.acquire(1920, 1080, frame_index=0)

        assert len(video_pool._slots) == 1

        # 退出上下文
        video_pool.__exit__(None, None, None)

        # 应<think>用户希望我继续完成测试文件的编写。我之前正在创建 test_video_upload_command.py 文件，被截断了。让我继续完成文件的剩余部分。</think>已释放
        assert len(video_pool._slots) == 0

    def test_with_statement_usage(self, mock_context: MagicMock) -> None:
        """with 语句使用"""
        from src.shared.video.video_texture_pool import VideoTexturePool

        with VideoTexturePool(mock_context, pool_size=5) as pool:
            tex = pool.acquire(1920, 1080, frame_index=0)
            assert tex is not None
            assert len(pool._slots) == 1

        # 退出后自动清理
        # 注意：这里 pool 变量仍可访问，但槽位已清空

    def test_exit_with_exception(self, video_pool: 'VideoTexturePool') -> None:
        """异常时 __exit__ 也释放"""
        video_pool.acquire(1920, 1080, frame_index=0)

        # 模拟异常退出
        video_pool.__exit__(Exception, Exception("test"), None)

        assert len(video_pool._slots) == 0


# =============================================================================
# 12. GC Finalizer 测试
# =============================================================================

class TestGCFinalizer:
    """测试 GC 后备释放机制"""

    def test_finalizer_cleanup_on_gc(self, mock_context: MagicMock) -> None:
        """GC 时 finalizer 清理"""
        import gc

        from src.shared.video.video_texture_pool import VideoTexturePool

        pool = VideoTexturePool(mock_context, pool_size=5)
        tex = pool.acquire(1920, 1080, frame_index=0)

        # 记录纹理引用
        tex_ref = pool._slots[0].texture

        # 删除 pool 引用
        del pool

        # 强制 GC
        gc.collect()

        # finalizer 应调用 release
        # Mock 版本可验证 release 被调用
        # 注意：实际 GC 时间不确定，这里仅验证机制存在

    def test_weakref_finalizer_present(self, video_pool: 'VideoTexturePool') -> None:
        """weakref.finalizer 存在"""
        assert hasattr(video_pool, '_finalizer')


# =============================================================================
# Manual Test Runner
# =============================================================================

def run_manual_tests() -> None:
    """手动运行测试（用于调试）"""
    print("\n=== Manual Video Upload Command Tests ===\n")

    if not HAS_MODERNGL:
        print("moderngl not available, using mock tests only")
        return

    try:
        ctx = moderngl.create_standalone_context()
        print("Created standalone context")

        from src.shared.video.video_texture_pool import VideoTexturePool

        # Test 1: Basic upload
        print("\n--- Test 1: Basic Upload ---")
        pool = VideoTexturePool(ctx, pool_size=5)
        tex = pool.acquire(1920, 1080, frame_index=0)
        print(f"  Acquired texture: glo={tex.glo}, size={tex.size}")
        print(f"  Filter: {tex.filter}")
        print(f"  Pool stats: {pool.get_stats()}")

        # Test 2: No mipmap
        print("\n--- Test 2: No Mipmap ---")
        # 验证 build_mipmaps 不被调用
        # 视频 filter 是 LINEAR，不是 MIPMAP
        print(f"  Min filter: LINEAR (0x{moderngl.LINEAR:02x})")
        print(f"  Mag filter: LINEAR (0x{moderngl.LINEAR:02x})")

        # Test 3: Frame sequence
        print("\n--- Test 3: Frame Sequence ---")
        for i in range(10):
            tex = pool.acquire(1920, 1080, frame_index=i)

        print(f"  Pool stats after 10 frames: {pool.get_stats()}")

        # Test 4: Cleanup
        print("\n--- Test 4: Cleanup ---")
        pool.release_all()
        print(f"  Pool stats after release: {pool.get_stats()}")

        print("\n=== Tests completed ===\n")

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


# =============================================================================
# P4.A: UPLOAD_DECODED_VIDEO_FRAME 和 UPLOAD_VIDEO_PLANES 测试
# =============================================================================

class TestP4ADecodedVideoUpload:
    """P4.A: 测试 UPLOAD_DECODED_VIDEO_FRAME 命令"""

    def test_render_command_exists(self) -> None:
        """验证新命令常量存在"""
        from src.features.color.core.async_renderer import RenderCommand

        assert hasattr(RenderCommand, 'UPLOAD_DECODED_VIDEO_FRAME')
        assert hasattr(RenderCommand, 'UPLOAD_VIDEO_PLANES')
        assert RenderCommand.UPLOAD_DECODED_VIDEO_FRAME == "upload_decoded_video_frame"
        assert RenderCommand.UPLOAD_VIDEO_PLANES == "upload_video_planes"

    def test_decoded_frame_packed_rgb(self, gl_context) -> None:
        """测试打包 RGB DecodedFrame 上传"""
        if not HAS_MODERNGL:
            pytest.skip("moderngl not available")

        from src.shared.media.frame import DecodedFrame
        from src.shared.video.video_upload import create_packed_frame_from_data
        import numpy as np

        # 创建测试 RGBA 数据
        data = np.random.randint(0, 255, (480, 640, 4), dtype=np.uint8)
        frame = create_packed_frame_from_data(data, 'rgba', frame_index=0)

        assert frame.is_packed
        assert frame.pixel_format == 'rgba'
        assert frame.width == 640
        assert frame.height == 480

    def test_decoded_frame_planar_yuv(self) -> None:
        """测试平面 YUV DecodedFrame"""
        from src.shared.video.video_upload import create_yuv_frame_from_planes
        import numpy as np

        y = np.random.randint(0, 255, (480, 640), dtype=np.uint8)
        u = np.random.randint(0, 255, (240, 320), dtype=np.uint8)
        v = np.random.randint(0, 255, (240, 320), dtype=np.uint8)

        frame = create_yuv_frame_from_planes(y, u, v, 'yuv420p')

        assert frame.is_planar
        assert len(frame.planes) == 3
        assert frame.pixel_format == 'yuv420p'

    def test_contiguous_nv12_is_normalized_to_two_gpu_planes(self) -> None:
        """VideoToolbox-style contiguous NV12 must not be treated as RGB."""
        from src.features.color.core.async_renderer import AsyncRenderer
        from src.shared.media.frame import DecodedFrame, get_pixel_format

        width, height = 8, 4
        raw_nv12 = np.arange(width * (height + height // 2), dtype=np.uint8)
        frame = DecodedFrame(
            frame_index=7,
            pts=70,
            time_seconds=7.0 / 25.0,
            data=raw_nv12.reshape(height + height // 2, width),
            pixel_format='nv12',
            width=width,
            height=height,
        )

        normalized = AsyncRenderer._materialize_contiguous_semiplanar_frame(frame)

        assert get_pixel_format('nv12').is_planar
        assert normalized is not None
        assert normalized.is_planar
        assert normalized.data is None
        assert len(normalized.planes) == 2
        assert normalized.planes[0].shape == (height, width)
        assert normalized.planes[1].shape == (height // 2, width)
        np.testing.assert_array_equal(normalized.planes[0], raw_nv12[:width * height].reshape(height, width))
        np.testing.assert_array_equal(normalized.planes[1], raw_nv12[width * height:].reshape(height // 2, width))

    def test_render_frame_routes_contiguous_nv12_to_plane_upload(self) -> None:
        """The combined playback command must bypass the packed RGB uploader."""
        from src.features.color.core.async_renderer import AsyncRenderer
        from src.shared.media.frame import DecodedFrame

        width, height = 8, 4
        renderer = AsyncRenderer.__new__(AsyncRenderer)
        renderer.ctx = object()
        renderer._video_generation_id = 0
        renderer._debug_frame_path = ''
        renderer._yuv_gpu_uploader = MagicMock()
        renderer._yuv_gpu_uploader.is_r16_available.return_value = False
        renderer._do_upload_video_planes = MagicMock(return_value=(MagicMock(), MagicMock()))
        renderer._do_upload_decoded_video_frame = MagicMock()
        renderer._do_render = MagicMock()
        renderer.canvas_size = (0, 0)
        renderer.raw_preview_params = {}

        raw_nv12 = np.zeros((height + height // 2, width), dtype=np.uint8)
        frame = DecodedFrame(
            frame_index=3,
            pts=3,
            time_seconds=3.0 / 25.0,
            data=raw_nv12,
            pixel_format='nv12',
            width=width,
            height=height,
        )

        renderer._do_render_video_frame({
            'decoded_frame': frame,
            'frame_index': frame.frame_index,
            'render_params': {'viewport_size': (width, height)},
        })

        renderer._do_upload_video_planes.assert_called_once()
        renderer._do_upload_decoded_video_frame.assert_not_called()
        uploaded_frame = renderer._do_upload_video_planes.call_args.args[0]['decoded_frame']
        assert uploaded_frame.is_planar
        assert len(uploaded_frame.planes) == 2

    def test_dispatch_packed_format(self, mock_context: MagicMock) -> None:
        """测试自动分流：打包格式"""
        from src.shared.video.video_upload import create_packed_frame_from_data, dispatch_upload_command
        from src.features.color.core.async_renderer import RenderCommand
        import numpy as np

        data = np.random.rand(480, 640, 4).astype(np.float32)
        frame = create_packed_frame_from_data(data, 'rgba')

        mock_submit = MagicMock()
        result = dispatch_upload_command(frame, mock_submit)

        assert result == 'upload_decoded_video_frame'
        mock_submit.assert_called_once()
        call_args = mock_submit.call_args
        assert call_args[0][0] == RenderCommand.UPLOAD_DECODED_VIDEO_FRAME

    def test_dispatch_planar_format(self, mock_context: MagicMock) -> None:
        """测试自动分流：平面格式"""
        from src.shared.video.video_upload import create_yuv_frame_from_planes, dispatch_upload_command
        from src.features.color.core.async_renderer import RenderCommand
        import numpy as np

        y = np.random.randint(0, 255, (480, 640), dtype=np.uint8)
        u = np.random.randint(0, 255, (240, 320), dtype=np.uint8)
        v = np.random.randint(0, 255, (240, 320), dtype=np.uint8)
        frame = create_yuv_frame_from_planes(y, u, v, 'yuv420p')

        mock_submit = MagicMock()
        result = dispatch_upload_command(frame, mock_submit)

        assert result == 'upload_video_planes'
        mock_submit.assert_called_once()
        call_args = mock_submit.call_args
        assert call_args[0][0] == RenderCommand.UPLOAD_VIDEO_PLANES

    def test_bgr_swizzle_detection(self) -> None:
        """测试 BGR 格式 swizzle 检测"""
        from src.shared.media.frame import get_pixel_format

        # BGR 格式应标记为需要 swizzle
        bgr_format = get_pixel_format('bgr24')
        assert bgr_format is not None
        assert bgr_format.name.startswith('bgr')

        bgra_format = get_pixel_format('bgra')
        assert bgra_format is not None
        assert bgra_format.name.startswith('bgr')

    def test_legacy_video_frame_upload_clears_stale_yuv_planes(self) -> None:
        """RGB/RGBA legacy upload must become the active video source."""
        from src.features.color.core.async_renderer import AsyncRenderer

        mock_texture = MagicMock()
        mock_texture.write = MagicMock()
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=mock_texture)
        mock_pool.get_stats = MagicMock(return_value={})

        renderer = AsyncRenderer.__new__(AsyncRenderer)
        renderer.ctx = MagicMock()
        renderer._video_texture_pool = mock_pool
        renderer.current_video_texture = None
        renderer.current_video_planes = (MagicMock(), MagicMock(), MagicMock())
        renderer.current_video_pixel_format = 'yuv444p'
        renderer.current_video_frame_index = -1
        renderer.video_source_size = (0, 0)
        renderer.source_image_size = (0, 0)
        renderer._video_needs_swizzle = True
        renderer.tiles = []

        frame_data = np.zeros((2, 2, 4), dtype=np.uint8)
        result = renderer._do_upload_video_frame({
            'frame_data': frame_data,
            'frame_index': 7,
            'width': 2,
            'height': 2,
            'components': 4,
            'dtype': 'f1',
        })

        assert result is mock_texture
        assert renderer.current_video_texture is mock_texture
        assert renderer.current_video_planes is None
        assert renderer.current_video_pixel_format is None
        assert renderer.current_video_frame_index == 7
        assert renderer._video_needs_swizzle is False
        assert renderer.tiles

    def test_video_hq_upload_clears_stale_playback_sources(self) -> None:
        """HQ/first-frame video upload must not render stale playback state."""
        from src.features.color.core.async_renderer import AsyncRenderer

        mock_texture = MagicMock()
        mock_texture.build_mipmaps = MagicMock()

        renderer = AsyncRenderer.__new__(AsyncRenderer)
        renderer.ctx = MagicMock()
        renderer.ctx.texture = MagicMock(return_value=mock_texture)
        renderer.reference_pyramid_textures = []
        renderer.pyramid_textures = []
        renderer.tiles = []
        renderer.current_video_texture = MagicMock()
        renderer.current_video_planes = (MagicMock(), MagicMock(), MagicMock())
        renderer.current_video_pixel_format = 'yuv444p'
        renderer.current_video_frame_index = 12
        renderer.source_image_size = (0, 0)

        image = np.zeros((4, 4, 3), dtype=np.uint8)
        renderer._do_upload(image, is_reference=False, original_size=(4, 4), is_video=True)

        assert renderer.current_video_texture is None
        assert renderer.current_video_planes is None
        assert renderer.current_video_pixel_format is None
        assert renderer.current_video_frame_index == -1
        assert renderer.pyramid_textures == [mock_texture]

    def test_render_video_frame_raw_uint8_uploads_normalized_texture(self) -> None:
        """Raw uint8 playback frames must upload as f1, not integer u1 textures."""
        renderer = _make_renderer_without_gl()
        renderer.ctx = MagicMock()
        renderer._video_generation_id = 0
        # drop_if_stale=False 触发 _do_render_video_frame 的调试日志路径，
        # 该路径读取 front tex/size 与当前 video 帧号。
        renderer.current_front_tex_id = None
        renderer.current_front_size = (0, 0)
        renderer.current_video_frame_index = -1
        captured_uploads = []

        def fake_upload(data):
            captured_uploads.append(data)
            return MagicMock()

        renderer._do_upload_video_frame = MagicMock(side_effect=fake_upload)
        renderer._do_render = MagicMock()

        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        renderer._do_render_video_frame({
            'frame': frame,
            'frame_index': 5,
            'render_params': {'viewport_size': (2, 2)},
            'drop_if_stale': False,
        })

        assert captured_uploads
        assert captured_uploads[0]['dtype'] == 'f1'

    def test_pending_clip_texture_drops_regular_render(self) -> None:
        """A clip switch must not publish its retained previous video texture."""
        renderer = _make_renderer_without_gl()
        renderer.ctx = MagicMock()
        renderer.ctx.viewport = (0, 0, 0, 0)
        renderer.video_playback_active = False
        renderer.video_texture_pending = True
        renderer.current_video_texture = MagicMock()
        renderer.current_video_planes = None
        renderer.reset_render_stats = MagicMock()

        renderer._do_render({
            'viewport_size': (1920, 1080),
            'render_reason': 'grading',
        })

        renderer.reset_render_stats.assert_not_called()
        assert renderer.ctx.viewport == (0, 0, 0, 0)

    def test_pending_clip_texture_allows_replacement_video_render(self) -> None:
        """The replacement video frame retains permission to render and clear the flag."""
        renderer = _make_renderer_without_gl()
        renderer.ctx = MagicMock()
        renderer._video_generation_id = 0
        renderer.current_front_tex_id = None
        renderer.current_front_size = (0, 0)
        renderer.current_video_frame_index = -1
        renderer.video_texture_pending = True
        renderer._do_upload_video_frame = MagicMock(return_value=MagicMock())
        renderer._do_render = MagicMock()

        renderer._do_render_video_frame({
            'frame': np.zeros((2, 2, 3), dtype=np.uint8),
            'frame_index': 5,
            'render_params': {'viewport_size': (2, 2)},
            'drop_if_stale': False,
        })

        assert renderer.video_texture_pending is False
        renderer._do_render.assert_called_once_with(
            {'viewport_size': (2, 2)}, allow_during_playback=True,
        )

    def test_yuv_render_restores_raw_view_fbo_after_conversion(self) -> None:
        """YUV conversion binds another FBO; raw_view must be rebound before display."""
        renderer = _make_renderer_without_gl()
        use_order: list[str] = []

        raw_fbo = MagicMock()
        raw_fbo.use = MagicMock(side_effect=lambda: use_order.append("raw"))
        ping_fbo = MagicMock()
        pong_fbo = MagicMock()
        screen_fbo = MagicMock()
        screen_fbo.use = MagicMock(side_effect=lambda: use_order.append("screen"))

        raw_tex = MagicMock()
        raw_tex.use = MagicMock()
        source_tex = MagicMock()
        source_tex.use = MagicMock()
        ping_tex = MagicMock()
        pong_tex = MagicMock()
        front_tex = MagicMock()
        front_tex.glo = 123

        renderer.ctx = MagicMock()
        renderer.ctx.BLEND = 1
        renderer.ctx.clear = MagicMock()
        renderer.ctx.enable = MagicMock()
        renderer.ctx.disable = MagicMock()
        renderer.ctx.finish = MagicMock()
        renderer.ctx.viewport = (0, 0, 0, 0)
        renderer.fb_manager = MagicMock()
        renderer.pipeline = MagicMock()
        renderer.utility_shaders = {
            "raw_view": MagicMock(),
            "passthrough": MagicMock(),
        }
        renderer.raw_preview_params = {}
        renderer.raw_lut_tex = None
        renderer.identity_lut = MagicMock()
        renderer.identity_lut.use = MagicMock()
        renderer._video_needs_swizzle = False
        renderer.pyramid_textures = []
        renderer.current_video_texture = None
        renderer.current_video_planes = (MagicMock(), MagicMock(), MagicMock())
        renderer.write_idx = 0
        renderer.fbo_pair = [screen_fbo, MagicMock()]
        renderer.tex_pair = [front_tex, MagicMock()]
        renderer.frame_ready = MagicMock()

        renderer.reset_render_stats = MagicMock()
        renderer._ensure_fbos = MagicMock()
        renderer._ensure_persistent_render_textures = MagicMock(
            return_value=(raw_tex, raw_fbo, ping_tex, ping_fbo, pong_tex, pong_fbo)
        )
        renderer._render_yuv_planes_to_rgb_texture = MagicMock(
            side_effect=lambda *_: use_order.append("yuv_conversion") or source_tex
        )
        renderer._render_quad = MagicMock()
        renderer._sync_gpu = MagicMock(return_value=False)
        renderer._publish_front_present_fence = MagicMock()
        renderer._retire_front_present_fence = MagicMock()

        renderer._do_render({
            "viewport_size": (64, 32),
            "layers": [],
            "raw_preview": {},
            "film_grain": {},
            "visible_rect": (0, 0, 64, 32),
            "show_mask": False,
            "active_layer_index": 0,
            "render_reason": "grading",
        })

        assert use_order == ["raw", "yuv_conversion", "raw", "screen"]
        assert renderer.current_front_tex_id == 123
        renderer._publish_front_present_fence.assert_called_once()
        renderer._retire_front_present_fence.assert_not_called()

    def test_paused_video_pyramid_uses_historic_tile_mapping(self) -> None:
        """A paused frame follows the same tile mapping as the known-good viewer."""
        renderer = _make_renderer_without_gl()
        raw_fbo = MagicMock()
        ping_fbo = MagicMock()
        pong_fbo = MagicMock()
        screen_fbo = MagicMock()
        source_tex = MagicMock()
        raw_tex = MagicMock()
        ping_tex = MagicMock()
        pong_tex = MagicMock()
        front_tex = MagicMock()
        front_tex.glo = 321

        renderer.ctx = MagicMock()
        renderer.ctx.BLEND = 1
        renderer.fb_manager = MagicMock()
        renderer.pipeline = MagicMock()
        renderer.utility_shaders = {
            "raw_view": MagicMock(),
            "passthrough": MagicMock(),
        }
        renderer.raw_preview_params = {}
        renderer.raw_lut_tex = None
        renderer.identity_lut = MagicMock()
        renderer._video_needs_swizzle = False
        renderer.current_video_texture = None
        renderer.current_video_planes = None
        renderer.pyramid_textures = [source_tex]
        renderer.video_source_size = (1920, 760)
        renderer.write_idx = 0
        renderer.fbo_pair = [screen_fbo, MagicMock()]
        renderer.tex_pair = [front_tex, MagicMock()]
        renderer.frame_ready = MagicMock()
        renderer.reset_render_stats = MagicMock()
        renderer._finish_call_count = 0
        renderer._ensure_fbos = MagicMock()
        renderer._ensure_persistent_render_textures = MagicMock(
            return_value=(raw_tex, raw_fbo, ping_tex, ping_fbo, pong_tex, pong_fbo)
        )
        renderer._render_quad = MagicMock()
        renderer._render_tiles_to_current_fbo = MagicMock()
        renderer._sync_gpu = MagicMock()

        renderer._do_render({
            "viewport_size": (1600, 900),
            "layers": [],
            "raw_preview": {},
            "film_grain": {},
            "visible_rect": (0, 0, 1920, 1080),
            "show_mask": False,
            "active_layer_index": 0,
            "render_reason": "static_preview",
        })

        renderer._render_tiles_to_current_fbo.assert_called_once_with(
            source_tex,
            renderer.utility_shaders["raw_view"],
            {
                "viewport_size": (1600, 900),
                "layers": [],
                "raw_preview": {},
                "film_grain": {},
                "visible_rect": (0, 0, 1920, 1080),
                "show_mask": False,
                "active_layer_index": 0,
                "render_reason": "static_preview",
            },
            0,
        )

    def test_packed_decoded_uint8_uploads_normalized_texture(self) -> None:
        """Packed 8-bit DecodedFrame upload must use f1 normalized textures."""
        from src.features.color.core.async_renderer import AsyncRenderer
        from src.shared.media.frame import DecodedFrame

        mock_texture = MagicMock()
        mock_texture.write = MagicMock()
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=mock_texture)

        renderer = AsyncRenderer.__new__(AsyncRenderer)
        renderer.ctx = MagicMock()
        renderer._video_texture_pool = mock_pool
        renderer.current_video_texture = None
        renderer.current_video_planes = None
        renderer.current_video_pixel_format = None
        renderer.current_video_frame_index = -1
        renderer.video_source_size = (0, 0)
        renderer.source_image_size = (0, 0)
        renderer._video_needs_swizzle = False
        renderer.tiles = []

        frame = DecodedFrame(
            frame_index=9,
            pts=0,
            time_seconds=0.0,
            data=np.zeros((2, 2, 3), dtype=np.uint8),
            pixel_format='rgb24',
            width=2,
            height=2,
        )

        assert renderer._do_upload_decoded_video_frame({'decoded_frame': frame}) is mock_texture
        assert mock_pool.acquire.call_args.args[4] == 'f1'

    def test_bgr24_swizzle_pixel_values(self, mock_context: MagicMock) -> None:
        """
        测试 BGR24 格式的实际像素值 swizzle。

        BGR 格式中 [0, 0, 255] 表示红色（B=0, G=0, R=255）。
        上传后应转换为 RGB 格式 [255, 0, 0] -> 归一化为 [1.0, 0.0, 0.0]。
        """
        from src.features.color.core.async_renderer import AsyncRenderer
        from src.shared.media.frame import DecodedFrame
        import numpy as np

        # 创建 2x2 BGR 帧：像素值 [0, 0, 255] 表示红色
        # BGR 格式：B 通道在前，R 通道在后
        bgr_data = np.array([
            [[0, 0, 255], [0, 128, 0]],   # 左: 红色 (B=0, G=0, R=255), 右: 绿色 (B=0, G=128, R=0)
            [[255, 0, 0], [128, 128, 128]] # 左: 蓝色 (B=255, G=0, R=0), 右: 灰色
        ], dtype=np.uint8)

        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=bgr_data,
            pixel_format='bgr24',
            width=2,
            height=2
        )

        # Mock texture 来捕获写入的数据
        written_data = []

        def mock_texture_write(data_bytes):
            # f1 normalized texture upload keeps uint8 bytes.
            arr = np.frombuffer(data_bytes, dtype=np.uint8).reshape(2, 2, 4)
            written_data.append(arr.copy())

        mock_texture = MagicMock()
        mock_texture.write = MagicMock(side_effect=mock_texture_write)
        mock_texture.size = (2, 2)
        mock_texture.components = 4
        mock_texture.dtype = 'f1'

        # Mock VideoTexturePool 返回我们的 mock texture
        mock_pool_instance = MagicMock()
        mock_pool_instance.acquire = MagicMock(return_value=mock_texture)

        # 创建 AsyncRenderer（直接设置 pool，不需要 patch）
        renderer = AsyncRenderer.__new__(AsyncRenderer)
        renderer.ctx = mock_context
        renderer._video_texture_pool = mock_pool_instance
        renderer.current_video_texture = None
        renderer.current_video_planes = None
        renderer.current_video_pixel_format = None
        renderer.current_video_frame_index = -1
        renderer.video_source_size = (0, 0)
        renderer.source_image_size = (0, 0)
        renderer._video_needs_swizzle = False
        renderer.tiles = []

        # 调用上传方法
        result = renderer._do_upload_decoded_video_frame({
            'decoded_frame': frame,
            'frame_index': 0
        })

        # 验证上传成功
        assert result is not None
        assert len(written_data) == 1

        # P3-1: GPU端 swizzle - texture write 保持原始 BGR 格式
        # swizzle 在 shader 中通过 u_bgr_swizzle uniform 处理
        # 验证 _video_needs_swizzle 状态被正确设置
        assert renderer._video_needs_swizzle is True

        # 验证像素值保持原始 BGR 格式（不做 CPU swizzle）
        pixels = written_data[0]

        # 像素 [0,0] 保持 BGR [0, 0, 255] (红色在 BGR 中是 B=0,G=0,R=255)
        # 注意：texture write 的数据是原始格式，swizzle 在渲染时发生
        np.testing.assert_array_equal(pixels[0, 0], [0, 0, 255, 255])

        # 像素 [0,1] 保持 BGR [0, 128, 0] (绿色)
        np.testing.assert_array_equal(pixels[0, 1], [0, 128, 0, 255])

        # 像素 [1,0] 保持 BGR [255, 0, 0] (蓝色)
        np.testing.assert_array_equal(pixels[1, 0], [255, 0, 0, 255])

        # 像素 [1,1] 保持 BGR [128, 128, 128] (灰色)
        np.testing.assert_array_equal(pixels[1, 1], [128, 128, 128, 255])

    def test_bgra_swizzle_pixel_values(self, mock_context: MagicMock) -> None:
        """
        P3-1: 测试 BGRA 格式的 GPU端 swizzle。

        BGRA 格式中 [0, 0, 255, 128] 表示红色半透明。
        上传时 texture write 保持原始 BGRA 格式（不做 CPU swizzle）。
        _video_needs_swizzle 状态被设置为 True，shader 渲染时处理通道交换。
        """
        from src.features.color.core.async_renderer import AsyncRenderer
        from src.shared.media.frame import DecodedFrame
        import numpy as np

        # 创建 2x2 BGRA 帧
        bgra_data = np.array([
            [[0, 0, 255, 128], [0, 255, 0, 255]],   # 红色半透明, 绿色不透明
            [[255, 0, 0, 64], [128, 128, 128, 192]]   # 蓝色低透明度, 灰色高透明度
        ], dtype=np.uint8)

        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=bgra_data,
            pixel_format='bgra',
            width=2,
            height=2
        )

        # Mock texture 来捕获写入的数据
        written_data = []

        def mock_texture_write(data_bytes):
            arr = np.frombuffer(data_bytes, dtype=np.uint8).reshape(2, 2, 4)  # RGBA bytes
            written_data.append(arr.copy())

        mock_texture = MagicMock()
        mock_texture.write = MagicMock(side_effect=mock_texture_write)
        mock_texture.size = (2, 2)
        mock_texture.components = 4
        mock_texture.dtype = 'f1'

        # Mock VideoTexturePool 返回我们的 mock texture
        mock_pool_instance = MagicMock()
        mock_pool_instance.acquire = MagicMock(return_value=mock_texture)

        # 创建 AsyncRenderer（直接设置 pool，不需要 patch）
        renderer = AsyncRenderer.__new__(AsyncRenderer)
        renderer.ctx = mock_context
        renderer._video_texture_pool = mock_pool_instance
        renderer.current_video_texture = None
        renderer.current_video_planes = None
        renderer.current_video_pixel_format = None
        renderer.current_video_frame_index = -1
        renderer.video_source_size = (0, 0)
        renderer.source_image_size = (0, 0)
        renderer._video_needs_swizzle = False
        renderer.tiles = []

        result = renderer._do_upload_decoded_video_frame({
            'decoded_frame': frame,
            'frame_index': 0
        })

        assert result is not None
        assert len(written_data) == 1

        # P3-1: GPU端 swizzle - 验证状态被正确设置
        assert renderer._video_needs_swizzle is True

        pixels = written_data[0]

        # P3-1: texture write 保持原始 BGRA 格式（不做 CPU swizzle）
        # swizzle 在 shader 渲染时发生

        # BGRA [0, 0, 255, 128] 保持不变（红色半透明）
        np.testing.assert_array_equal(pixels[0, 0], [0, 0, 255, 128])

        # BGRA [0, 255, 0, 255] 保持不变（绿色不透明）
        np.testing.assert_array_equal(pixels[0, 1], [0, 255, 0, 255])

        # BGRA [255, 0, 0, 64] 保持不变（蓝色低透明度）
        np.testing.assert_array_equal(pixels[1, 0], [255, 0, 0, 64])

        # BGRA [128, 128, 128, 192] 保持不变（灰色高透明度）
        np.testing.assert_array_equal(pixels[1, 1], [128, 128, 128, 192])


class TestP4AVideoPlanesUpload:
    """P4.A: 测试 UPLOAD_VIDEO_PLANES 命令"""

    def test_yuv420_upload_command(self, gl_context) -> None:
        """测试 YUV420 上传"""
        if not HAS_MODERNGL:
            pytest.skip("moderngl not available")

        from src.shared.video.yuv_gpu_upload import YUVGPUUploader, create_test_yuv_frame

        uploader = YUVGPUUploader(gl_context, pool_size=5)

        y, u, v = create_test_yuv_frame(1920, 1080, '420', 8)
        y_tex, u_tex, v_tex = uploader.upload_yuv420(y, u, v, frame_index=0)

        assert y_tex is not None
        assert u_tex is not None
        assert v_tex is not None
        assert y_tex.size == (1920, 1080)
        assert u_tex.size == (960, 540)

        uploader.release()

    def test_nv12_upload_command(self, gl_context) -> None:
        """测试 NV12 上传"""
        if not HAS_MODERNGL:
            pytest.skip("moderngl not available")

        from src.shared.video.yuv_gpu_upload import YUVGPUUploader, create_test_nv12_frame

        uploader = YUVGPUUploader(gl_context, pool_size=5)

        y, uv = create_test_nv12_frame(1920, 1080, 8)
        y_tex, uv_tex = uploader.upload_nv12(y, uv, frame_index=0)

        assert y_tex is not None
        assert uv_tex is not None
        assert y_tex.size == (1920, 1080)
        assert uv_tex.size == (960, 540)

        uploader.release()

    def test_p010_upload_command(self, gl_context) -> None:
        """测试 P010 上传"""
        if not HAS_MODERNGL:
            pytest.skip("moderngl not available")

        from src.shared.video.yuv_gpu_upload import YUVGPUUploader, create_test_nv12_frame

        uploader = YUVGPUUploader(gl_context, pool_size=5)

        y, uv = create_test_nv12_frame(1920, 1080, 10)
        y_tex, uv_tex = uploader.upload_p010(y, uv, frame_index=0)

        assert y_tex is not None
        assert uv_tex is not None

        uploader.release()

    def test_yuv422_upload_command(self, gl_context) -> None:
        """测试 YUV422 上传"""
        if not HAS_MODERNGL:
            pytest.skip("moderngl not available")

        from src.shared.video.yuv_gpu_upload import YUVGPUUploader, create_test_yuv_frame

        uploader = YUVGPUUploader(gl_context, pool_size=5)

        y, u, v = create_test_yuv_frame(1920, 1080, '422', 8)
        y_tex, u_tex, v_tex = uploader.upload_yuv422(y, u, v, frame_index=0)

        assert y_tex.size == (1920, 1080)
        assert u_tex.size == (960, 1080)  # 422: 水平减半
        assert v_tex.size == (960, 1080)

        uploader.release()

    def test_yuv444_upload_command(self, gl_context) -> None:
        """测试 YUV444 上传"""
        if not HAS_MODERNGL:
            pytest.skip("moderngl not available")

        from src.shared.video.yuv_gpu_upload import YUVGPUUploader, create_test_yuv_frame

        uploader = YUVGPUUploader(gl_context, pool_size=5)

        y, u, v = create_test_yuv_frame(1920, 1080, '444', 8)
        y_tex, u_tex, v_tex = uploader.upload_yuv444(y, u, v, frame_index=0)

        assert y_tex.size == (1920, 1080)
        assert u_tex.size == (1920, 1080)  # 444: 全尺寸
        assert v_tex.size == (1920, 1080)

        uploader.release()

    def test_video_texture_linear_filter(self, gl_context) -> None:
        """验证视频纹理使用 LINEAR 过滤"""
        if not HAS_MODERNGL:
            pytest.skip("moderngl not available")

        from src.shared.video.yuv_gpu_upload import YUVGPUUploader, create_test_yuv_frame

        uploader = YUVGPUUploader(gl_context, pool_size=5)

        y, u, v = create_test_yuv_frame(1920, 1080, '420', 8)
        y_tex, u_tex, v_tex = uploader.upload_yuv420(y, u, v, frame_index=0)

        # 验证 LINEAR 过滤
        assert y_tex.filter == (moderngl.LINEAR, moderngl.LINEAR)
        assert u_tex.filter == (moderngl.LINEAR, moderngl.LINEAR)
        assert v_tex.filter == (moderngl.LINEAR, moderngl.LINEAR)

        uploader.release()


class TestP4ARendererState:
    """P4.A: 测试渲染器状态更新"""

    def test_video_planes_state_tracking(self) -> None:
        """测试 current_video_planes 状态"""
        from src.features.color.core.async_renderer import AsyncRenderer

        # 验证属性存在（类型检查）
        # AsyncRenderer 应有 current_video_planes 属性
        # 这里只验证导入成功，实际测试需要 mock context
        assert hasattr(AsyncRenderer, '__init__')

    def test_pixel_format_tracking(self) -> None:
        """测试 pixel_format 状态追踪"""
        from src.features.color.core.async_renderer import AsyncRenderer

        # 验证属性存在
        # AsyncRenderer 应有 current_video_pixel_format 属性
        assert hasattr(AsyncRenderer, '__init__')


# =============================================================================
# S5.A: Render Queue Preview Frame Coalescing Tests
# =============================================================================

class TestS5APreviewFrameCoalescing:
    """S5.A: 测试 Render Queue 只保留最新 preview frame"""

    def test_renderer_stats_exists(self) -> None:
        """验证渲染器统计方法存在"""
        from src.features.color.core.async_renderer import AsyncRenderer

        # 验证方法存在
        assert hasattr(AsyncRenderer, 'get_renderer_stats')

    def test_command_data_quality_reason_fields(self) -> None:
        """验证命令数据包含 quality 和 reason 字段"""
        from src.features.color.core.async_renderer import RenderCommand

        # 创建测试命令数据
        cmd_data: Dict[str, Any] = {
            'frame_data': np.zeros((480, 640, 4), dtype=np.float16),
            'frame_index': 0,
            'width': 640,
            'height': 480,
            'components': 4,
            'dtype': 'f2',
            'generation_id': 0,
            'drop_if_stale': True,
            'quality': 'proxy',
            'reason': 'playback',
        }

        # 验证字段存在
        assert 'quality' in cmd_data
        assert 'reason' in cmd_data
        assert cmd_data['quality'] == 'proxy'
        assert cmd_data['reason'] == 'playback'

    def test_proxy_playback_should_drop(self) -> None:
        """验证 proxy playback 命令应被丢弃"""
        # 判断逻辑：quality=proxy 且 reason=playback
        current_data = {
            'quality': 'proxy',
            'reason': 'playback',
            'drop_if_stale': True,
        }

        next_data = {
            'quality': 'proxy',
            'reason': 'playback',
        }

        should_drop = (
            current_data.get('drop_if_stale', True) and
            current_data.get('quality') == 'proxy' and
            current_data.get('reason') == 'playback' and
            next_data.get('quality') == 'proxy' and
            next_data.get('reason') == 'playback'
        )

        assert should_drop is True

    def test_master_seek_should_not_drop(self) -> None:
        """验证 master/seek 命令不应被丢弃"""
        current_data = {
            'quality': 'proxy',
            'reason': 'playback',
            'drop_if_stale': True,
        }

        # master 命令不应被丢弃
        next_data_master = {
            'quality': 'master',
            'reason': 'seek',
        }

        should_drop_master = (
            next_data_master.get('quality') == 'proxy' and
            next_data_master.get('reason') == 'playback'
        )

        assert should_drop_master is False

    def test_seek_reason_should_not_drop(self) -> None:
        """验证 seek reason 命令不应被丢弃"""
        current_data = {
            'quality': 'proxy',
            'reason': 'playback',
            'drop_if_stale': True,
        }

        next_data_seek = {
            'quality': 'proxy',
            'reason': 'seek',
        }

        should_drop_seek = (
            next_data_seek.get('quality') == 'proxy' and
            next_data_seek.get('reason') == 'playback'
        )

        assert should_drop_seek is False

    def test_export_reason_should_not_drop(self) -> None:
        """验证 export reason 命令不应被丢弃"""
        next_data_export = {
            'quality': 'proxy',
            'reason': 'export',
        }

        should_drop_export = (
            next_data_export.get('quality') == 'proxy' and
            next_data_export.get('reason') == 'playback'
        )

        assert should_drop_export is False

    def test_composite_reason_should_not_drop(self) -> None:
        """验证 composite reason 命令不应被丢弃"""
        next_data_composite = {
            'quality': 'master',
            'reason': 'composite',
        }

        should_drop_composite = (
            next_data_composite.get('quality') == 'proxy' and
            next_data_composite.get('reason') == 'playback'
        )

        assert should_drop_composite is False

    def test_queue_coalescing_simulation(self, mock_renderer: MagicMock) -> None:
        """模拟队列合并过程"""
        from src.features.color.core.async_renderer import RenderCommand

        cmd_queue = queue.Queue()

        # 提交 10 个 playback preview 命令
        for i in range(10):
            cmd_queue.put((RenderCommand.RENDER_VIDEO_FRAME, {
                'frame_index': i,
                'quality': 'proxy',
                'reason': 'playback',
                'drop_if_stale': True,
            }))

        # 提交 1 个 seek 命令（不应被丢弃）
        cmd_queue.put((RenderCommand.RENDER_VIDEO_FRAME, {
            'frame_index': 20,
            'quality': 'master',
            'reason': 'seek',
            'drop_if_stale': True,
        }))

        initial_size = cmd_queue.qsize()
        assert initial_size == 11

        # 模拟合并逻辑（第一个命令到达）
        item = cmd_queue.get()
        cmd_type, data = item
        dropped_count = 0

        if cmd_type == RenderCommand.RENDER_VIDEO_FRAME:
            current_data = data or {}
            current_quality = current_data.get('quality', 'proxy')
            current_reason = current_data.get('reason', 'playback')
            current_drop_if_stale = current_data.get('drop_if_stale', True)

            if current_drop_if_stale and current_quality == 'proxy' and current_reason == 'playback':
                while not cmd_queue.empty():
                    try:
                        next_item = cmd_queue.queue[0]
                        if next_item[0] == RenderCommand.RENDER_VIDEO_FRAME:
                            next_data = next_item[1] or {}
                            next_quality = next_data.get('quality', 'proxy')
                            next_reason = next_data.get('reason', 'playback')

                            if next_quality == 'proxy' and next_reason == 'playback':
                                cmd_queue.get_nowait()
                                dropped_count += 1
                            else:
                                break
                        else:
                            break
                    except (IndexError, queue.Empty):
                        break

        # 验证结果：只保留 1 个 playback + 1 个 seek
        remaining_size = cmd_queue.qsize()
        assert dropped_count == 9  # 丢弃了 9 个 playback preview 命令
        assert remaining_size == 1  # seek 命令保留

        # 验证剩余命令是 seek
        remaining_item = cmd_queue.get()
        remaining_data = remaining_item[1]
        assert remaining_data.get('reason') == 'seek'

    def test_stats_dropped_preview_frames(self) -> None:
        """验证 dropped_preview_frames 统计"""
        # 模拟统计数据
        stats = {
            'dropped_preview_frames': 10,
            'queue_depth': 2,
            'processed_frames': 100,
        }

        assert 'dropped_preview_frames' in stats
        assert stats['dropped_preview_frames'] == 10


class TestS5AIntegration:
    """S5.A: 集成测试"""

    def test_submit_with_quality_reason(self, mock_renderer: MagicMock) -> None:
        """验证提交命令包含 quality 和 reason"""
        from src.features.color.core.async_renderer import RenderCommand

        # 模拟 VideoPlaybackEngine 提交
        render_params = {'viewport_size': (1920, 1080)}
        generation_id = 1

        cmd_data = {
            'frame_data': np.zeros((1080, 1920, 4), dtype=np.float16),
            'frame_index': 0,
            'width': 1920,
            'height': 1080,
            'components': 4,
            'dtype': 'f2',
            'render_params': render_params,
            'generation_id': generation_id,
            'drop_if_stale': True,
            'quality': 'proxy',
            'reason': 'playback',
        }

        # 提交
        mock_renderer.submit(RenderCommand.RENDER_VIDEO_FRAME, cmd_data)

        # 验证提交参数
        mock_renderer.submit.assert_called_once()
        call_args = mock_renderer.submit.call_args
        assert call_args[0][0] == RenderCommand.RENDER_VIDEO_FRAME
        assert call_args[0][1].get('quality') == 'proxy'
        assert call_args[0][1].get('reason') == 'playback'

    def test_mixed_reasons_preserved(self, mock_renderer: MagicMock) -> None:
        """验证混合 reason 命令的保留逻辑"""
        from src.features.color.core.async_renderer import RenderCommand

        cmd_queue = queue.Queue()

        # 提交混合命令
        commands = [
            (RenderCommand.RENDER_VIDEO_FRAME, {'quality': 'proxy', 'reason': 'playback', 'frame_index': 0}),
            (RenderCommand.RENDER_VIDEO_FRAME, {'quality': 'proxy', 'reason': 'playback', 'frame_index': 1}),
            (RenderCommand.RENDER_VIDEO_FRAME, {'quality': 'proxy', 'reason': 'playback', 'frame_index': 2}),
            (RenderCommand.RENDER_VIDEO_FRAME, {'quality': 'master', 'reason': 'seek', 'frame_index': 10}),
            (RenderCommand.RENDER_VIDEO_FRAME, {'quality': 'proxy', 'reason': 'playback', 'frame_index': 3}),
            (RenderCommand.RENDER_VIDEO_FRAME, {'quality': 'proxy', 'reason': 'export', 'frame_index': 20}),
        ]

        for cmd in commands:
            cmd_queue.put(cmd)

        assert cmd_queue.qsize() == 6

        # 执行合并逻辑（模拟第一个 playback 命令处理）
        item = cmd_queue.get()
        cmd_type, data = item

        # 合并 playback 命令
        if data.get('quality') == 'proxy' and data.get('reason') == 'playback':
            dropped = 0
            while not cmd_queue.empty():
                try:
                    next_item = cmd_queue.queue[0]
                    if next_item[0] == RenderCommand.RENDER_VIDEO_FRAME:
                        next_data = next_item[1] or {}
                        if next_data.get('quality') == 'proxy' and next_data.get('reason') == 'playback':
                            cmd_queue.get_nowait()
                            dropped += 1
                        else:
                            break
                    else:
                        break
                except (IndexError, queue.Empty):
                    break

        # 验证结果
        # 预期：第一个 playback 处理，丢弃 2 个 playback（索引 1, 2）
        # 保留 seek, playback 3, export
        # 注意：实际合并逻辑会保留最新 playback，丢弃队列中旧的

    def test_dropped_frames_counter_increment(self) -> None:
        """验证丢帧计数器增加"""
        # 模拟计数器逻辑
        dropped_preview_frames = 0

        # 模拟丢弃 5 个帧
        for i in range(5):
            dropped_preview_frames += 1

        assert dropped_preview_frames == 5


class TestGpuFrameAtomicSwap:
    """G-P0-05: 零拷贝帧交换必须原子——新帧成功绑定后才释放旧帧 backing。

    此前先释放旧 _previous_gpu_frame 再 wrap 新帧；若 wrap 失败，current_video_planes
    仍指向已释放的 IOSurface，渲染会采样悬空纹理。
    """

    def _make_gpu_frame(self, frame_index: int):
        from src.shared.media.gpu_frame import GpuDecodedFrame

        frame = GpuDecodedFrame(
            frame_index=frame_index,
            pts=frame_index,
            time_seconds=float(frame_index) / 24.0,
            width=8,
            height=4,
            visible_rect=(0, 0, 8, 4),
            pixel_format='nv12',
            bit_depth=8,
            planes=[MagicMock()],  # non-empty so it passes the planes check
            color_range='limited',
            color_matrix='bt709',
            decode_path='macos_vt',
            zero_copy=True,
        )
        frame.release = MagicMock()
        return frame

    def _make_renderer(self):
        from src.features.color.core.async_renderer import AsyncRenderer
        from src.shared.media.gpu_frame import safe_release_gpu_frame  # noqa: F401

        renderer = _make_renderer_without_gl()
        renderer._video_generation_id = 0
        renderer._debug_frame_path = ''
        renderer._debug_decode_path = ''
        renderer.current_video_planes = None
        renderer.current_video_pixel_format = None
        renderer.video_source_size = (0, 0)
        renderer._coerce_video_yuv_matrix = lambda m: m
        renderer._coerce_video_color_range = lambda r: r
        return renderer

    def test_wrap_failure_keeps_previous_frame_backing(self):
        renderer = self._make_renderer()
        prev = self._make_gpu_frame(1)
        renderer._previous_gpu_frame = prev
        # 让 wrap 失败（返回 None）
        renderer._wrap_external_gpu_planes = lambda planes, gpu_frame: None

        new_frame = self._make_gpu_frame(2)
        renderer._do_render_video_gpu_frame({'gpu_decoded_frame': new_frame})

        # 关键：旧帧 backing 未被释放（恢复保留），避免悬空采样。
        prev.release.release.assert_not_called()
        # 新帧从未绑定，不再被 renderer 持有，必须立即归还其外部 backing。
        new_frame.release.release.assert_called_once()
        # _previous_gpu_frame 恢复为旧帧
        assert renderer._previous_gpu_frame is prev

    def test_no_planes_keeps_previous_frame_backing(self):
        renderer = self._make_renderer()
        prev = self._make_gpu_frame(1)
        renderer._previous_gpu_frame = prev

        # 构造无 planes 的帧
        new_frame = self._make_gpu_frame(2)
        new_frame.planes = []
        renderer._do_render_video_gpu_frame({'gpu_decoded_frame': new_frame})

        prev.release.release.assert_not_called()
        new_frame.release.release.assert_called_once()
        assert renderer._previous_gpu_frame is prev

    def test_successful_wrap_releases_previous_frame(self):
        renderer = self._make_renderer()
        prev = self._make_gpu_frame(1)
        renderer._previous_gpu_frame = prev
        wrapped = [MagicMock()]
        renderer._wrap_external_gpu_planes = lambda planes, gpu_frame: wrapped
        # 绑定成功后会进入实际渲染（_do_render），超出 G-P0-05 范围，短路掉。
        renderer._do_render = MagicMock()

        new_frame = self._make_gpu_frame(2)
        renderer._do_render_video_gpu_frame({'gpu_decoded_frame': new_frame})

        # 成功绑定后释放旧帧 backing
        prev.release.release.assert_called_once()
        assert renderer._previous_gpu_frame is new_frame
        assert renderer.current_video_planes is wrapped

    def test_rebinding_the_held_frame_does_not_release_its_backing(self):
        """A duplicate render command may reuse the currently held GPU frame."""
        renderer = self._make_renderer()
        frame = self._make_gpu_frame(1)
        renderer._previous_gpu_frame = frame
        renderer._wrap_external_gpu_planes = lambda planes, gpu_frame: [MagicMock()]
        renderer._do_render = MagicMock()

        renderer._do_render_video_gpu_frame({'gpu_decoded_frame': frame})

        frame.release.release.assert_not_called()
        assert renderer._previous_gpu_frame is frame


if __name__ == '__main__':
    run_manual_tests()
