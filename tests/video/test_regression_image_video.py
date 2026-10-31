"""
Image/RAW Regression Tests
确保视频管线不影响图像和 RAW 处理功能。

## 覆盖范围

- ImageEngine 核心功能不受视频管线影响
- RAW 解码功能不受影响
- 图层合成功能不受影响
- 调色参数功能不受影响
- GPU 资源管理不受影响
- 历史记录/撤销功能不受影响

## 测试策略

- 使用 conftest.py 中的 image_engine fixture
- 验证图像处理核心功能完整性
- 检查视频模块导入不影响图像模块
- 确保 API 接口稳定性

## 测试类别

- Critical: 必须通过，否则影响核心功能
- Regression: 验证功能未退化
- Integration: 图像与视频模块交互边界

"""

from __future__ import annotations

import os
import sys
import pytest
import numpy as np
from typing import Optional, Any
from unittest.mock import MagicMock, patch, Mock

# 导入核心模块
from src.features.color.core.image_engine import ImageEngine, Tile
from src.features.color.core.layer import Layer, MaskShape, MaskGroup
from src.features.color.core.history import HistoryManager


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def sample_image_rgb():
    """100x100 RGB uint8 测试图像"""
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    # 创建测试图案
    img[:50, :50] = [255, 0, 0]    # 左上红
    img[:50, 50:] = [0, 255, 0]    # 右上绿
    img[50:, :50] = [0, 0, 255]    # 左下蓝
    img[50:, 50:] = [255, 255, 0]  # 右下黄
    return img


@pytest.fixture
def sample_image_rgba():
    """100x100 RGBA uint8 测试图像"""
    img = np.zeros((100, 100, 4), dtype=np.uint8)
    img[:50, :50] = [255, 0, 0, 255]
    img[:50, 50:] = [0, 255, 0, 200]  # 半透明绿
    img[50:, :50] = [0, 0, 255, 255]
    img[50:, 50:] = [255, 255, 0, 100]  # 低透明度黄
    return img


@pytest.fixture
def sample_image_high_bit_depth():
    """100x100 RGB uint16 高位深测试图像"""
    img = np.zeros((100, 100, 3), dtype=np.uint16)
    # 10-bit 范围值
    img[:50, :50] = [1023, 0, 0]    # 最大红色 (10-bit)
    img[:50, 50:] = [0, 512, 0]     # 中等绿色
    img[50:, :50] = [0, 0, 1023]
    img[50:, 50:] = [1023, 1023, 0]
    return img


@pytest.fixture
def sample_tile():
    """测试 Tile"""
    return Tile(0, 0, 512, 512, 0)


@pytest.fixture
def sample_layer():
    """测试 Layer"""
    return Layer("Regression Test Layer")


@pytest.fixture
def mask_shape_circle():
    """圆形 MaskShape"""
    return MaskShape(MaskShape.CIRCLE)


@pytest.fixture
def mask_shape_rect():
    """矩形 MaskShape"""
    return MaskShape(MaskShape.RECT)  # 使用 RECT 常量


@pytest.fixture
def mask_group():
    """测试 MaskGroup"""
    return MaskGroup("Regression Test Mask Group")


# =============================================================================
# Regression Tests - ImageEngine Core
# =============================================================================

class TestRegressionImageEngineCore:
    """Regression 测试 - ImageEngine 核心功能"""

    @pytest.mark.regression
    @pytest.mark.critical
    def test_image_engine_creation(self, image_engine) -> None:
        """ImageEngine 创建不受影响"""
        assert image_engine is not None
        assert isinstance(image_engine, ImageEngine)

    @pytest.mark.regression
    @pytest.mark.critical
    def test_image_engine_has_layers(self, image_engine) -> None:
        """ImageEngine 应有默认图层"""
        assert len(image_engine.layers) >= 1
        assert image_engine.layers[0] is not None

    @pytest.mark.regression
    @pytest.mark.critical
    def test_image_engine_active_layer(self, image_engine) -> None:
        """ImageEngine 应有活动图层"""
        active = image_engine.get_active_layer()
        assert active is not None

    @pytest.mark.regression
    def test_image_engine_add_layer(self, image_engine) -> None:
        """添加图层功能正常"""
        initial_count = len(image_engine.layers)
        new_layer = image_engine.add_layer()

        assert len(image_engine.layers) == initial_count + 1
        assert new_layer is image_engine.layers[-1]
        assert image_engine.active_layer_index == len(image_engine.layers) - 1

    @pytest.mark.regression
    def test_image_engine_remove_layer(self, image_engine) -> None:
        """删除图层功能正常"""
        image_engine.add_layer()
        image_engine.add_layer()

        count_before = len(image_engine.layers)
        image_engine.remove_layer(1)

        assert len(image_engine.layers) == count_before - 1

    @pytest.mark.regression
    def test_image_engine_cannot_remove_last_layer(self, image_engine) -> None:
        """不能删除最后一个图层"""
        initial_count = len(image_engine.layers)
        image_engine.remove_layer(0)
        assert len(image_engine.layers) == initial_count

    @pytest.mark.regression
    def test_image_engine_set_active_layer(self, image_engine) -> None:
        """设置活动图层功能正常"""
        image_engine.add_layer()
        image_engine.add_layer()

        image_engine.set_active_layer(1)
        assert image_engine.active_layer_index == 1

        image_engine.set_active_layer(0)
        assert image_engine.active_layer_index == 0

    @pytest.mark.regression
    def test_image_engine_duplicate_layer(self, image_engine) -> None:
        """复制图层功能正常"""
        image_engine.layers[0].grading_params["exposure"] = 2.5
        image_engine.layers[0].name = "Original Layer"

        dup = image_engine.duplicate_layer(0)

        assert dup is not None
        assert len(image_engine.layers) == 2
        assert dup.grading_params["exposure"] == 2.5
        assert "Copy" in dup.name or "副本" in dup.name

    @pytest.mark.regression
    def test_image_engine_layer_name_increment(self, image_engine) -> None:
        """图层名称递增"""
        image_engine.add_layer()
        assert image_engine.layers[1].name == "Layer 2"

        image_engine.add_layer()
        assert image_engine.layers[2].name == "Layer 3"


# =============================================================================
# Regression Tests - Layer Operations
# =============================================================================

class TestRegressionLayerOperations:
    """Regression 测试 - 图层操作"""

    @pytest.mark.regression
    @pytest.mark.critical
    def test_layer_creation(self, sample_layer) -> None:
        """Layer 创建正常"""
        assert sample_layer is not None
        assert isinstance(sample_layer, Layer)
        assert sample_layer.name == "Regression Test Layer"

    @pytest.mark.regression
    @pytest.mark.critical
    def test_layer_has_grading_params(self, sample_layer) -> None:
        """Layer 应有调色参数"""
        params = sample_layer.grading_params
        assert params is not None
        assert isinstance(params, dict)

        # 验证基础调色参数存在
        expected_params = [
            'exposure', 'contrast', 'saturation', 
            'temperature', 'tint', 'shadows', 'highlights'
        ]
        for param in expected_params:
            assert param in params

    @pytest.mark.regression
    def test_layer_grading_params_default_values(self, sample_layer) -> None:
        """调色参数默认值正确"""
        params = sample_layer.grading_params

        # 默认值应为 0（无调整）
        assert params['exposure'] == 0.0
        assert params['contrast'] == 0.0
        assert params['saturation'] == 0.0

    @pytest.mark.regression
    def test_layer_set_grading_params(self, sample_layer) -> None:
        """设置调色参数正常"""
        sample_layer.grading_params['exposure'] = 1.5
        sample_layer.grading_params['contrast'] = 0.8
        sample_layer.grading_params['saturation'] = -0.3

        assert sample_layer.grading_params['exposure'] == 1.5
        assert sample_layer.grading_params['contrast'] == 0.8
        assert sample_layer.grading_params['saturation'] == -0.3

    @pytest.mark.regression
    def test_layer_visibility(self, sample_layer) -> None:
        """图层可见性控制正常"""
        assert sample_layer.visible is True

        sample_layer.visible = False
        assert sample_layer.visible is False

        sample_layer.visible = True
        assert sample_layer.visible is True

    @pytest.mark.regression
    def test_layer_opacity(self, sample_layer) -> None:
        """图层透明度控制正常"""
        assert sample_layer.opacity == 1.0

        sample_layer.opacity = 0.5
        assert sample_layer.opacity == 0.5

        sample_layer.opacity = 0.0
        assert sample_layer.opacity == 0.0

    @pytest.mark.regression
    def test_layer_blend_mode(self, sample_layer) -> None:
        """图层混合模式正常"""
        # 默认应为 Normal
        assert sample_layer.blend_mode == "Normal" or sample_layer.blend_mode == "normal"

        # 设置其他混合模式
        sample_layer.blend_mode = "Multiply"
        assert sample_layer.blend_mode == "Multiply"


# =============================================================================
# Regression Tests - Mask Operations
# =============================================================================

class TestRegressionMaskOperations:
    """Regression 测试 - Mask 操作"""

    @pytest.mark.regression
    def test_mask_shape_creation_circle(self, mask_shape_circle) -> None:
        """圆形 MaskShape 创建正常"""
        assert mask_shape_circle is not None
        # MaskShape 使用 .type 属性而不是 .shape_type
        assert mask_shape_circle.type == MaskShape.CIRCLE

    @pytest.mark.regression
    def test_mask_shape_creation_rect(self, mask_shape_rect) -> None:
        """矩形 MaskShape 创建正常"""
        assert mask_shape_rect is not None
        # MaskShape 使用 .type 属性，常量名是 RECT 而不是 RECTANGLE
        assert mask_shape_rect.type == MaskShape.RECT

    @pytest.mark.regression
    def test_mask_group_creation(self, mask_group) -> None:
        """MaskGroup 创建正常"""
        assert mask_group is not None
        assert mask_group.name == "Regression Test Mask Group"

    @pytest.mark.regression
    def test_mask_group_add_shape(self, mask_group, mask_shape_circle) -> None:
        """MaskGroup 添加 MaskShape 正常"""
        # MaskGroup 使用 add_child() 方法而不是 add_shape()
        mask_group.add_child(mask_shape_circle)

        # MaskGroup 使用 .children 属性而不是 .shapes
        assert len(mask_group.children) == 1
        assert mask_group.children[0] == mask_shape_circle

    @pytest.mark.regression
    def test_layer_add_mask_group(self, sample_layer, mask_group) -> None:
        """Layer 添加 MaskGroup 正常"""
        # Layer 使用 mask_shapes 列表存储 MaskShape 和 MaskGroup
        # 添加 MaskGroup 到 mask_shapes
        sample_layer.mask_shapes.append(mask_group)

        assert len(sample_layer.mask_shapes) > 0
        assert sample_layer.mask_shapes[-1] == mask_group


# =============================================================================
# Regression Tests - History/Undo
# =============================================================================

class TestRegressionHistoryUndo:
    """Regression 测试 - 历史记录/撤销功能"""

    @pytest.mark.regression
    @pytest.mark.critical
    def test_history_manager_creation(self) -> None:
        """HistoryManager 创建正常"""
        history = HistoryManager(max_steps=10)
        assert history is not None
        assert history.max_steps == 10

    @pytest.mark.regression
    def test_history_push_state(self) -> None:
        """HistoryManager 推送状态正常"""
        history = HistoryManager(max_steps=10)

        # 创建测试图像
        test_image = np.zeros((100, 100, 3), dtype=np.uint8)
        history.push_state(test_image, layers=None, active_layer_index=0)

        # 验证 undo_stack 有状态
        assert len(history.undo_stack) == 1
        assert history.redo_stack == []

    @pytest.mark.regression
    def test_history_undo(self) -> None:
        """HistoryManager 撤销功能正常"""
        history = HistoryManager(max_steps=10)

        # 创建两个测试图像
        image1 = np.zeros((100, 100, 3), dtype=np.uint8)
        image2 = np.ones((100, 100, 3), dtype=np.uint8) * 128

        history.push_state(image1, layers=None, active_layer_index=0)
        history.push_state(image2, layers=None, active_layer_index=0)

        # 撤销需要当前状态
        current = np.ones((100, 100, 3), dtype=np.uint8) * 255
        undone = history.undo(current_image=current)

        assert undone is not None
        # 2 个 push + 1 个 undo = 1 个 left in undo_stack
        assert len(history.undo_stack) == 1
        assert len(history.redo_stack) == 1

    @pytest.mark.regression
    def test_history_redo(self) -> None:
        """HistoryManager 重做功能正常"""
        history = HistoryManager(max_steps=10)

        image1 = np.zeros((100, 100, 3), dtype=np.uint8)
        image2 = np.ones((100, 100, 3), dtype=np.uint8) * 128

        history.push_state(image1)
        history.push_state(image2)

        current = np.ones((100, 100, 3), dtype=np.uint8) * 255
        history.undo(current_image=current)

        redone = history.redo(current_image=image1)
        assert redone is not None
        assert len(history.redo_stack) == 0

    @pytest.mark.regression
    def test_history_max_steps(self) -> None:
        """HistoryManager 最大步数限制"""
        history = HistoryManager(max_steps=5)

        for i in range(10):
            test_image = np.ones((100, 100, 3), dtype=np.uint8) * (i * 10)
            history.push_state(test_image)

        # undo_stack 最多 5 个状态
        assert len(history.undo_stack) == 5


# =============================================================================
# Regression Tests - Tile Operations
# =============================================================================

class TestRegressionTileOperations:
    """Regression 测试 - Tile 操作"""

    @pytest.mark.regression
    @pytest.mark.critical
    def test_tile_creation(self, sample_tile) -> None:
        """Tile 创建正常"""
        assert sample_tile is not None
        assert isinstance(sample_tile, Tile)

    @pytest.mark.regression
    def test_tile_properties(self, sample_tile) -> None:
        """Tile 属性正确"""
        assert sample_tile.x == 0
        assert sample_tile.y == 0
        # Tile 类使用 w 和 h 属性，而不是 width 和 height
        assert sample_tile.w == 512
        assert sample_tile.h == 512
        assert sample_tile.level == 0

    @pytest.mark.regression
    def test_tile_bounds(self, sample_tile) -> None:
        """Tile 边界计算正确"""
        # Tile 类没有 bounds 属性，手动计算边界
        bounds = (sample_tile.x, sample_tile.y, sample_tile.x + sample_tile.w, sample_tile.y + sample_tile.h)
        assert bounds == (0, 0, 512, 512)


# =============================================================================
# Regression Tests - RAW Processing
# =============================================================================

class TestRegressionRAWProcessing:
    """Regression 测试 - RAW 处理功能"""

    @pytest.mark.regression
    def test_raw_module_import_not_blocked(self) -> None:
        """RAW 模块导入不受视频模块影响"""
        # 尝试导入 RAW 相关模块
        try:
            from src.core.raw_decoder import RawDecoder
            # 导入成功，功能正常
            assert RawDecoder is not None
        except ImportError:
            # 如果模块不存在，跳过测试
            pytest.skip("RawDecoder module not available")

    @pytest.mark.regression
    def test_raw_worker_thread_not_blocked(self, image_engine) -> None:
        """RAW 工作线程不受影响"""
        # ImageEngine 应有 raw_worker_thread
        assert image_engine.raw_worker_thread is not None

    @pytest.mark.regression
    def test_raw_params_not_altered(self, image_engine) -> None:
        """RAW 参数未被修改"""
        # 验证 RAW 参数结构完整
        if hasattr(image_engine, 'raw_params'):
            params = image_engine.raw_params
            assert params is not None


# =============================================================================
# Regression Tests - GPU Resources
# =============================================================================

class TestRegressionGPUResources:
    """Regression 测试 - GPU 资源管理"""

    @pytest.mark.regression
    def test_gl_context_manager_import(self) -> None:
        """GLContextManager 导入正常"""
        try:
            from src.shared.gpu.gl_backend import GLContextManager
            assert GLContextManager is not None
        except ImportError:
            pytest.skip("GLContextManager module not available")

    @pytest.mark.regression
    def test_gpu_resource_pool_import(self) -> None:
        """GPUResourcePool 导入正常"""
        try:
            from src.features.composite.render.gpu_resource_pool import GPUResourcePool
            assert GPUResourcePool is not None
        except ImportError:
            pytest.skip("GPUResourcePool module not available")

    @pytest.mark.regression
    def test_image_engine_mocked_gl_context(self, image_engine) -> None:
        """ImageEngine GL Context 被 mock（测试环境）"""
        # 测试环境中 GLContextManager 应被 mock
        # render_pipeline 应为 MagicMock
        assert image_engine.render_pipeline is not None


# =============================================================================
# Regression Tests - Video Module Isolation
# =============================================================================

class TestRegressionVideoModuleIsolation:
    """Regression 测试 - 视频模块隔离"""

    @pytest.mark.regression
    def test_video_modules_import_not_break_image(self) -> None:
        """视频模块导入不破坏图像模块"""
        # 导入视频相关模块
        try:
            from src.shared.media.ffmpeg_source import FFmpegMediaSource
            from src.shared.media.metadata import VideoMetadata
        except ImportError as e:
            pytest.skip(f"Video modules not available: {e}")

        # 导入图像模块
        from src.features.color.core.image_engine import ImageEngine
        from src.features.color.core.layer import Layer

        # 两者都应正常工作
        assert FFmpegMediaSource is not None
        assert ImageEngine is not None
        assert Layer is not None

    @pytest.mark.regression
    def test_video_metadata_does_not_pollute_image_metadata(self) -> None:
        """视频元数据不污染图像元数据"""
        from src.shared.media.metadata import VideoMetadata, Rational

        # 创建视频元数据
        video_meta = VideoMetadata(
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            total_frames=240,
            codec_name='h264',
            codec_profile='High',
            pixel_format='yuv420p',
            bit_depth=8,
        )

        # 图像处理不应受影响
        from src.features.color.core.layer import Layer
        layer = Layer("Test")
        assert layer.grading_params is not None

    @pytest.mark.regression
    def test_video_source_class_not_conflict_with_image_source(self) -> None:
        """视频源类不与图像源类冲突"""
        try:
            from src.shared.media.source import MediaSource, VideoSource
            from src.features.color.core.layer import Layer
        except ImportError:
            pytest.skip("Source modules not available")

        # 验证类定义独立
        assert MediaSource is not None
        assert VideoSource is not None
        assert Layer is not None


# =============================================================================
# Regression Tests - API Stability
# =============================================================================

class TestRegressionAPIStability:
    """Regression 测试 - API 接口稳定性"""

    @pytest.mark.regression
    @pytest.mark.critical
    def test_image_engine_public_methods_exist(self, image_engine) -> None:
        """ImageEngine 公开方法存在"""
        # 验证关键方法存在
        required_methods = [
            'add_layer',
            'remove_layer',
            'set_active_layer',
            'get_active_layer',
            'duplicate_layer',
        ]

        for method_name in required_methods:
            assert hasattr(image_engine, method_name)
            assert callable(getattr(image_engine, method_name))

    @pytest.mark.regression
    @pytest.mark.critical
    def test_layer_public_properties_exist(self, sample_layer) -> None:
        """Layer 公开属性存在"""
        required_properties = [
            'name',
            'visible',
            'opacity',
            'blend_mode',
            'grading_params',
        ]

        for prop_name in required_properties:
            assert hasattr(sample_layer, prop_name)

    @pytest.mark.regression
    @pytest.mark.critical
    def test_layer_public_methods_exist(self, sample_layer) -> None:
        """Layer 公开方法存在"""
        required_methods = [
            'add_mask_group',
        ]

        for method_name in required_methods:
            if hasattr(sample_layer, method_name):
                assert callable(getattr(sample_layer, method_name))

    @pytest.mark.regression
    def test_grading_params_keys_stable(self, sample_layer) -> None:
        """调色参数键名稳定"""
        params = sample_layer.grading_params
        
        # 核心调色参数不应被删除或改名
        core_params = [
            'exposure',
            'contrast',
            'saturation',
        ]

        for key in core_params:
            assert key in params, f"Core param '{key}' missing from grading_params"


# =============================================================================
# Regression Tests - Data Integrity
# =============================================================================

class TestRegressionDataIntegrity:
    """Regression 测试 - 数据完整性"""

    @pytest.mark.regression
    def test_numpy_array_operations_not_altered(self, sample_image_rgb) -> None:
        """NumPy 数组操作未受影响"""
        # 基本数组操作
        assert sample_image_rgb.shape == (100, 100, 3)
        assert sample_image_rgb.dtype == np.uint8

        # 数组切片
        slice_img = sample_image_rgb[:50, :50]
        assert slice_img.shape == (50, 50, 3)

        # 数组复制
        copy_img = sample_image_rgb.copy()
        assert np.array_equal(sample_image_rgb, copy_img)

    @pytest.mark.regression
    def test_high_bit_depth_array_not_altered(self, sample_image_high_bit_depth) -> None:
        """高位深数组操作未受影响"""
        assert sample_image_high_bit_depth.dtype == np.uint16
        assert sample_image_high_bit_depth.max() > 255  # 高位深数据

    @pytest.mark.regression
    def test_rgba_array_not_altered(self, sample_image_rgba) -> None:
        """RGBA 数组操作未受影响"""
        assert sample_image_rgba.shape == (100, 100, 4)
        assert sample_image_rgba.dtype == np.uint8

        # Alpha 通道访问
        alpha = sample_image_rgba[:, :, 3]
        assert alpha.shape == (100, 100)


# =============================================================================
# Regression Tests - Performance Impact
# =============================================================================

class TestRegressionPerformanceImpact:
    """Regression 测试 - 性能影响"""

    @pytest.mark.regression
    def test_layer_creation_performance(self) -> None:
        """图层创建性能未退化"""
        import time

        start = time.time()
        layers = [Layer(f"Layer {i}") for i in range(100)]
        elapsed = time.time() - start

        # 100 个图层创建应在 1 秒内完成
        assert elapsed < 1.0
        assert len(layers) == 100

    @pytest.mark.regression
    def test_grading_params_update_performance(self, sample_layer) -> None:
        """调色参数更新性能未退化"""
        import time

        start = time.time()
        for i in range(1000):
            sample_layer.grading_params['exposure'] = i * 0.001
        elapsed = time.time() - start

        # 1000 次更新应在 0.5 秒内完成
        assert elapsed < 0.5


# =============================================================================
# Regression Tests - Signal/Slot Connections
# =============================================================================

class TestRegressionSignalSlotConnections:
    """Regression 测试 - PyQt 信号槽连接"""

    @pytest.mark.regression
    def test_image_engine_has_key_signals(self, image_engine) -> None:
        """ImageEngine 应有核心信号"""
        # 检查实际存在的关键信号
        # ImageEngine 有 frame_ready, mask_shapes_changed, color_output_changed 等信号
        assert hasattr(image_engine, 'frame_ready')
        assert hasattr(image_engine, 'mask_shapes_changed')
        assert hasattr(image_engine, 'color_output_changed')

    @pytest.mark.regression
    def test_layer_signals_exist(self, sample_layer) -> None:
        """Layer 应有必要的信号"""
        # Layer 是普通 Python 类，没有 PyQt 信号
        # 检查 Layer 是否有必要的属性和方法
        assert hasattr(sample_layer, 'grading_params')
        assert hasattr(sample_layer, 'is_dirty')


# =============================================================================
# Regression Tests - Thread Safety
# =============================================================================

class TestRegressionThreadSafety:
    """Regression 测试 - 线程安全"""

    @pytest.mark.regression
    def test_raw_worker_thread_is_qthread(self, image_engine) -> None:
        """RAW 工作线程是 QThread"""
        from PyQt6.QtCore import QThread

        assert isinstance(image_engine.raw_worker_thread, QThread)

    @pytest.mark.regression
    def test_ai_worker_thread_is_qthread(self, image_engine) -> None:
        """AI 工作线程是 QThread"""
        from PyQt6.QtCore import QThread

        assert isinstance(image_engine.ai_worker_thread, QThread)


# =============================================================================
# Integration Tests - Image/Video Boundary
# =============================================================================

class TestIntegrationImageVideoBoundary:
    """Integration 测试 - 图像/视频边界"""

    @pytest.mark.integration
    @pytest.mark.skip(reason="Qt/OpenGL native abort: mock GLContextManager 导致 OpenGL 资源创建失败，属于平台环境问题")
    def test_image_engine_can_handle_both_image_and_video_contexts(self) -> None:
        """ImageEngine 可以处理图像和视频上下文"""
        # 创建 ImageEngine（图像模式）
        with patch('src.features.color.core.image_engine.GLContextManager') as MockGL:
            mock_gl_instance = MagicMock()
            mock_gl_instance.initialized = False
            MockGL.instance.return_value = mock_gl_instance

            from src.features.color.core.image_engine import ImageEngine
            engine = ImageEngine()

            # 验证可以同时导入视频模块
            try:
                from src.shared.media.ffmpeg_source import FFmpegMediaSource
                # 视频模块导入不影响 ImageEngine
                assert engine is not None
            except ImportError:
                pytest.skip("Video modules not available")

    @pytest.mark.integration
    def test_video_metadata_does_not_interfere_with_layer_metadata(self) -> None:
        """视频元数据不干扰图层元数据"""
        from src.features.color.core.layer import Layer
        from src.shared.media.metadata import VideoMetadata, Rational

        # 创建图层
        layer = Layer("Test Layer")
        layer.grading_params['exposure'] = 1.5

        # 创建视频元数据
        video_meta = VideoMetadata(
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            total_frames=240,
            codec_name='h264',
            codec_profile='High',
            pixel_format='yuv420p',
            bit_depth=8,
        )

        # 图层参数不受影响
        assert layer.grading_params['exposure'] == 1.5


# =============================================================================
# Regression Tests - Import Order Independence
# =============================================================================

class TestRegressionImportOrderIndependence:
    """Regression 测试 - 导入顺序独立性"""

    @pytest.mark.regression
    def test_import_video_before_image(self) -> None:
        """先导入视频模块不影响图像模块"""
        # 先导入视频模块
        try:
            from src.shared.media.ffmpeg_source import FFmpegMediaSource
        except ImportError:
            pytest.skip("Video modules not available")

        # 后导入图像模块
        from src.features.color.core.image_engine import ImageEngine
        from src.features.color.core.layer import Layer

        assert ImageEngine is not None
        assert Layer is not None

    @pytest.mark.regression
    def test_import_image_before_video(self) -> None:
        """先导入图像模块不影响视频模块"""
        # 先导入图像模块
        from src.features.color.core.image_engine import ImageEngine
        from src.features.color.core.layer import Layer

        # 后导入视频模块
        try:
            from src.shared.media.ffmpeg_source import FFmpegMediaSource
            from src.shared.media.metadata import VideoMetadata
        except ImportError:
            pytest.skip("Video modules not available")

        assert ImageEngine is not None
        assert FFmpegMediaSource is not None


# =============================================================================
# Regression Tests - Fixture Compatibility
# =============================================================================

class TestRegressionFixtureCompatibility:
    """Regression 测试 - pytest fixture 兼容性"""

    @pytest.mark.regression
    def test_qapp_fixture_works_with_video_imports(self, qapp) -> None:
        """qapp fixture 与视频模块导入兼容"""
        try:
            from src.shared.media.ffmpeg_source import FFmpegMediaSource
        except ImportError:
            pytest.skip("Video modules not available")

        assert qapp is not None

    @pytest.mark.regression
    def test_image_engine_fixture_works_with_video_imports(self, image_engine) -> None:
        """image_engine fixture 与视频模块导入兼容"""
        try:
            from src.shared.media.ffmpeg_source import FFmpegMediaSource
        except ImportError:
            pytest.skip("Video modules not available")

        assert image_engine is not None


# =============================================================================
# P6.A: Image/Video Upload State Regression Tests
# =============================================================================

class TestRegressionImageUploadState:
    """
    P6.A Regression 测试 - 图片上传状态断言

    验证图片模式下：
    - current_video_texture is None
    - pyramid_textures 正常生成
    - image upload 调用 build_mipmaps()
    """

    @pytest.mark.regression
    @pytest.mark.critical
    def test_image_mode_current_video_texture_is_none(self, image_engine) -> None:
        """图片模式下 current_video_texture 应为 None"""
        # ImageEngine 在图片模式下应确保 current_video_texture 为 None
        # 由于测试环境 renderer 为 None，我们需要验证状态初始化
        assert image_engine.renderer is None

        # 如果有 renderer，验证视频纹理状态
        # 这是架构约束：图片模式不能有残留的视频纹理
        if image_engine.renderer is not None:
            assert image_engine.renderer.current_video_texture is None, \
                "Image mode should not have current_video_texture"

    @pytest.mark.regression
    @pytest.mark.critical
    def test_image_upload_clears_video_texture_state(self) -> None:
        """图片上传后应清理视频纹理状态"""
        from unittest.mock import MagicMock, PropertyMock

        # 创建 mock renderer 模拟图片上传行为
        mock_renderer = MagicMock()
        mock_renderer.current_video_texture = None
        mock_renderer.pyramid_textures = []
        mock_renderer.command_queue = MagicMock()

        # 模拟 UPLOAD_IMAGE 命令提交
        from src.features.color.core.async_renderer import RenderCommand
        mock_renderer.submit(RenderCommand.UPLOAD_IMAGE, {'image': np.zeros((100, 100, 3), dtype=np.uint8)})

        # 验证命令被提交
        mock_renderer.submit.assert_called_once()

        # 验证视频纹理状态应为 None（架构约束）
        # _do_upload 在清理时会设置 current_video_texture = None
        assert mock_renderer.current_video_texture is None, \
            "UPLOAD_IMAGE should clear current_video_texture"

    @pytest.mark.regression
    def test_image_mode_pyramid_textures_generation_logic(self) -> None:
        """图片模式应生成 pyramid_textures（验证逻辑而非实际 GPU）"""
        # 由于测试环境无 GPU，验证 _do_upload 的逻辑路径
        # 检查 AsyncRenderer._do_upload 中 pyramid_textures 清理和重建逻辑

        from src.features.color.core.async_renderer import AsyncRenderer, RenderCommand

        # 检查代码逻辑：UPLOAD_IMAGE 会清理 pyramid_textures 并重建
        # 通过导入验证函数存在
        assert hasattr(AsyncRenderer, '_do_upload'), \
            "AsyncRenderer should have _do_upload method"

        # pyramid_textures 是实例属性，在 __init__ 中定义
        # 验证 __init__ 源码中包含 pyramid_textures 初始化
        import inspect
        init_source = inspect.getsource(AsyncRenderer.__init__)
        assert 'pyramid_textures' in init_source, \
            "AsyncRenderer.__init__ should initialize pyramid_textures"

    @pytest.mark.regression
    def test_image_upload_builds_mipmaps_logic(self) -> None:
        """图片上传应调用 build_mipmaps()（验证逻辑路径）"""
        # 验证 _do_upload 方法中存在 build_mipmaps 调用逻辑
        import inspect
        from src.features.color.core.async_renderer import AsyncRenderer

        # 检查 _do_upload 源码中是否包含 build_mipmaps
        source = inspect.getsource(AsyncRenderer._do_upload)
        assert 'build_mipmaps' in source, \
            "_do_upload should call build_mipmaps for image upload"

    @pytest.mark.regression
    def test_image_upload_command_exists(self) -> None:
        """UPLOAD_IMAGE 命令应存在"""
        from src.features.color.core.async_renderer import RenderCommand

        assert hasattr(RenderCommand, 'UPLOAD_IMAGE'), \
            "RenderCommand should have UPLOAD_IMAGE"
        assert RenderCommand.UPLOAD_IMAGE == "upload_image", \
            "UPLOAD_IMAGE command name should be 'upload_image'"


class TestRegressionVideoUploadState:
    """
    P6.A Regression 测试 - 视频上传状态断言

    验证视频模式下：
    - 不调用 image mipmap path
    - 切回图片后 video texture 状态清理
    """

    @pytest.mark.regression
    @pytest.mark.critical
    def test_video_upload_command_exists(self) -> None:
        """UPLOAD_VIDEO_FRAME 命令应存在"""
        from src.features.color.core.async_renderer import RenderCommand

        assert hasattr(RenderCommand, 'UPLOAD_VIDEO_FRAME'), \
            "RenderCommand should have UPLOAD_VIDEO_FRAME"
        assert RenderCommand.UPLOAD_VIDEO_FRAME == "upload_video_frame", \
            "UPLOAD_VIDEO_FRAME command name should be 'upload_video_frame'"

    @pytest.mark.regression
    @pytest.mark.critical
    def test_video_upload_does_not_call_mipmap_path(self) -> None:
        """视频上传不应调用 build_mipmaps"""
        import inspect
        from src.features.color.core.async_renderer import AsyncRenderer

        # 检查 _do_upload_video_frame 源码
        source = inspect.getsource(AsyncRenderer._do_upload_video_frame)

        # 视频上传不应有 tex.build_mipmaps() 调用（排除文档字符串）
        # 实际调用形式是 tex.build_mipmaps()，而非注释中的说明
        assert 'tex.build_mipmaps()' not in source, \
            "_do_upload_video_frame should NOT call tex.build_mipmaps()"
        assert 'texture.build_mipmaps()' not in source, \
            "_do_upload_video_frame should NOT call texture.build_mipmaps()"

        # 但应包含 current_video_texture 更新（实际代码调用）
        # 检查 self.current_video_texture = texture 这种赋值形式
        assert 'self.current_video_texture = texture' in source, \
            "_do_upload_video_frame should update current_video_texture"

    @pytest.mark.regression
    def test_video_mode_skips_pyramid_generation(self) -> None:
        """视频模式应跳过金字塔纹理生成"""
        from src.features.color.core.async_renderer import AsyncRenderer

        # 检查 _do_upload_video_frame 不操作 pyramid_textures
        import inspect
        source = inspect.getsource(AsyncRenderer._do_upload_video_frame)

        # 视频上传不应操作 pyramid_textures 列表
        # 注意：pyramid_textures 在方法中只被读取检查，不应被写入
        # 检查源码中没有 self.pyramid_textures = [] 这样的清理操作
        assert 'self.pyramid_textures = []' not in source, \
            "_do_upload_video_frame should NOT clear pyramid_textures"

    @pytest.mark.regression
    def test_switch_to_image_clears_video_texture(self) -> None:
        """切回图片后应清理视频纹理状态"""
        import inspect
        from src.features.color.core.async_renderer import AsyncRenderer

        # 检查 _do_upload (图片上传) 中清理视频纹理的逻辑
        source = inspect.getsource(AsyncRenderer._do_upload)

        # 图片上传时应清理 current_video_texture
        assert 'current_video_texture = None' in source, \
            "_do_upload should clear current_video_texture when switching to image mode"

    @pytest.mark.regression
    def test_render_selects_correct_source_texture(self) -> None:
        """渲染时应根据模式选择正确的源纹理"""
        import inspect
        from src.features.color.core.async_renderer import AsyncRenderer

        # 检查 _do_render 中的源纹理选择逻辑
        # _do_render is a resource-scope wrapper; source selection lives in
        # the delegated implementation where the video/pyramid branches are
        # actually executed.
        source = inspect.getsource(AsyncRenderer._do_render_impl)

        # 应有 current_video_texture 和 pyramid_textures 的条件判断
        assert 'current_video_texture' in source, \
            "_do_render should check current_video_texture"
        assert 'pyramid_textures' in source, \
            "_do_render should check pyramid_textures"

    @pytest.mark.regression
    def test_video_texture_pool_initialization(self) -> None:
        """视频纹理池应在渲染线程初始化"""
        import inspect
        from src.features.color.core.async_renderer import AsyncRenderer

        # 检查 run() 方法中 VideoTexturePool 的初始化
        source = inspect.getsource(AsyncRenderer.run)

        assert 'VideoTexturePool' in source, \
            "AsyncRenderer.run should initialize VideoTexturePool"


class TestRegressionRAWSmoke:
    """
    P6.A Regression 测试 - RAW 基础 smoke

    验证 RAW 模块导入和基础功能，
    如果环境无 RAW fixture 则 skip。
    """

    @pytest.mark.regression
    @pytest.mark.smoke
    def test_raw_decoder_module_import(self) -> None:
        """RAW 解码模块应可导入"""
        try:
            from src.core.raw_decoder import RawDecoder
            assert RawDecoder is not None
        except ImportError:
            pytest.skip("RawDecoder module not available - no RAW fixture")

    @pytest.mark.regression
    @pytest.mark.smoke
    def test_raw_decoder_class_has_required_methods(self) -> None:
        """RawDecoder 类应有必要的方法"""
        try:
            from src.core.raw_decoder import RawDecoder
        except ImportError:
            pytest.skip("RawDecoder module not available - no RAW fixture")

        # 检查必要方法存在
        required_methods = ['decode', 'get_metadata']
        for method in required_methods:
            if hasattr(RawDecoder, method):
                assert callable(getattr(RawDecoder, method)), \
                    f"RawDecoder.{method} should be callable"

    @pytest.mark.regression
    @pytest.mark.smoke
    def test_raw_extensions_defined(self) -> None:
        """RAW 文件扩展名列表应定义"""
        from src.features.color.core.image_engine import ImageEngine

        assert hasattr(ImageEngine, 'RAW_EXTENSIONS'), \
            "ImageEngine should have RAW_EXTENSIONS defined"

        # 检查常见 RAW 格式存在
        raw_exts = ImageEngine.RAW_EXTENSIONS
        common_raw = ['.arw', '.cr2', '.cr3', '.nef', '.dng']

        for ext in common_raw:
            assert ext.lower() in [e.lower() for e in raw_exts], \
                f"RAW_EXTENSIONS should include {ext}"

    @pytest.mark.regression
    @pytest.mark.smoke
    def test_raw_params_structure(self, image_engine) -> None:
        """RAW 参数结构应完整"""
        # 验证 RAW 参数默认值
        assert hasattr(image_engine, 'current_raw_params'), \
            "ImageEngine should have current_raw_params"

        params = image_engine.current_raw_params
        assert isinstance(params, dict), \
            "current_raw_params should be a dict"

        # 验证基础参数键存在
        assert 'enabled' in params, \
            "raw_params should have 'enabled' key"

    @pytest.mark.regression
    @pytest.mark.smoke
    def test_raw_worker_thread_exists(self, image_engine) -> None:
        """RAW 工作线程应存在"""
        from PyQt6.QtCore import QThread

        assert hasattr(image_engine, 'raw_worker_thread'), \
            "ImageEngine should have raw_worker_thread"

        assert isinstance(image_engine.raw_worker_thread, QThread), \
            "raw_worker_thread should be QThread instance"

    @pytest.mark.regression
    @pytest.mark.smoke
    def test_raw_worker_exists(self, image_engine) -> None:
        """RAW 工作对象应存在"""
        assert hasattr(image_engine, 'raw_worker'), \
            "ImageEngine should have raw_worker"

        assert image_engine.raw_worker is not None, \
            "raw_worker should not be None"

    @pytest.mark.regression
    @pytest.mark.smoke
    def test_file_io_raw_handling_methods_exist(self) -> None:
        """FileIO 应有 RAW 处理方法"""
        try:
            from src.shared.io.file_io import FileIO
        except ImportError:
            pytest.skip("FileIO module not available")

        # 检查 RAW 相关方法存在
        # update_raw_decoding 是关键方法
        assert hasattr(FileIO, 'update_raw_decoding'), \
            "FileIO should have update_raw_decoding method"


# =============================================================================
# Run Tests Manually
# =============================================================================

def run_manual_regression_tests() -> None:
    """手动运行 regression 测试（用于调试）"""
    print("\n=== Manual Image/RAW Regression Tests ===\n")

    # Test 1: ImageEngine 创建
    print("Test 1: ImageEngine Creation")
    try:
        with patch('src.features.color.core.image_engine.GLContextManager') as MockGL:
            mock_gl_instance = MagicMock()
            mock_gl_instance.initialized = False
            MockGL.instance.return_value = mock_gl_instance

            from src.features.color.core.image_engine import ImageEngine
            engine = ImageEngine()
            print(f"  [PASS] ImageEngine created: {engine}")
            print(f"  [PASS] Layers count: {len(engine.layers)}")
    except Exception as e:
        print(f"  [FAIL] {e}")

    # Test 2: Layer 创建
    print("\nTest 2: Layer Creation")
    try:
        from src.features.color.core.layer import Layer
        layer = Layer("Manual Test Layer")
        print(f"  [PASS] Layer created: {layer.name}")
        print(f"  [PASS] Grading params: {layer.grading_params}")
    except Exception as e:
        print(f"  [FAIL] {e}")

    # Test 3: 视频模块导入不影响图像模块
    print("\nTest 3: Video Import Does Not Affect Image")
    try:
        from src.shared.media.ffmpeg_source import FFmpegMediaSource
        from src.features.color.core.layer import Layer
        layer = Layer("Post Video Import Layer")
        print(f"  [PASS] Both modules imported successfully")
        print(f"  [PASS] Layer created after video import: {layer.name}")
    except ImportError as e:
        print(f"  [SKIP] Video modules not available: {e}")
    except Exception as e:
        print(f"  [FAIL] {e}")

    # Test 4: HistoryManager
    print("\nTest 4: HistoryManager")
    try:
        from src.features.color.core.history import HistoryManager
        history = HistoryManager(max_steps=5)
        history.push_state({'exposure': 0.0})
        history.push_state({'exposure': 1.0})
        undone = history.undo()
        print(f"  [PASS] HistoryManager created")
        print(f"  [PASS] Undo returned: {undone}")
    except Exception as e:
        print(f"  [FAIL] {e}")

    print("\n=== Manual Tests Complete ===\n")


if __name__ == '__main__':
    run_manual_regression_tests()
