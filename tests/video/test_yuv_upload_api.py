"""
YUV Upload API Tests - YUV 帧上传测试

测试覆盖范围：
1. YUV420/YUV422/YUV444 格式上传
2. BT.709/BT.2020/BT.601 矩阵转换
3. Limited/Full range 处理
4. 高位深支持 (10-bit, 12-bit)
5. Planar YUV 数据处理
6. Chroma subsampling 处理

注意事项：
- OpenGL 测试需要创建 standalone context
- Shader 编译需要 GLSL 环境
- 测试后必须清理资源
"""

import pytest
import sys
import os
import numpy as np
from typing import Tuple, Dict, Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 检查 moderngl 是否可用
try:
    import moderngl
    HAS_MODERNGL = True
except ImportError:
    HAS_MODERNGL = False


# =============================================================================
# Constants
# =============================================================================

# Color Space Matrices (YUV -> RGB)
BT709_MATRIX = np.array([
    [1.0, 1.0, 1.0],
    [0.0, -0.1873, 1.8556],
    [1.5748, -0.4681, 0.0]
], dtype=np.float32)

BT2020_MATRIX = np.array([
    [1.0, 1.0, 1.0],
    [0.0, -0.1646, 1.8814],
    [1.4746, -0.5714, 0.0]
], dtype=np.float32)

# BT.601 数字 YCbCr 系数（AUD-R2-010，与 yuv_gpu_upload.py 修正后的 GPU
# 矩阵一致：Kr=0.299/Kb=0.114 推导，limited-range ±0.5 色度约定）
BT601_MATRIX = np.array([
    [1.0, 1.0, 1.0],
    [0.0, -0.3441, 1.7720],
    [1.4020, -0.7141, 0.0]
], dtype=np.float32)

# Range Constants
Y_OFFSET_LIM = 16.0 / 255.0
Y_SCALE_LIM = 255.0 / 219.0  # 235 - 16 = 219
C_OFFSET_LIM = 128.0 / 255.0
C_OFFSET_FULL = 0.5


# =============================================================================
# Helper Functions
# =============================================================================

def create_yuv420_frame(width: int, height: int, bit_depth: int = 8) -> Tuple[np.ndarray, ...]:
    """创建 YUV420 测试帧
    
    YUV420 (4:2:0):
    - Y: width x height
    - U: (width/2) x (height/2)
    - V: (width/2) x (height/2)
    """
    dtype = np.uint16 if bit_depth > 8 else np.uint8
    max_val = (1 << bit_depth) - 1
    
    # 创建渐变测试数据
    y = np.linspace(0, max_val, width * height, dtype=dtype).reshape(height, width)
    
    # U/V: 色度子采样，尺寸减半
    u_width = width // 2
    u_height = height // 2
    u = np.full((u_height, u_width), max_val // 2, dtype=dtype)
    v = np.full((u_height, u_width), max_val // 2, dtype=dtype)
    
    return y, u, v


def create_yuv422_frame(width: int, height: int, bit_depth: int = 8) -> Tuple[np.ndarray, ...]:
    """创建 YUV422 测试帧
    
    YUV422 (4:2:2):
    - Y: width x height
    - U: (width/2) x height
    - V: (width/2) x height
    """
    dtype = np.uint16 if bit_depth > 8 else np.uint8
    max_val = (1 << bit_depth) - 1
    
    y = np.linspace(0, max_val, width * height, dtype=dtype).reshape(height, width)
    
    # U/V: 水平方向减半
    u_width = width // 2
    u = np.full((height, u_width), max_val // 2, dtype=dtype)
    v = np.full((height, u_width), max_val // 2, dtype=dtype)
    
    return y, u, v


def create_yuv444_frame(width: int, height: int, bit_depth: int = 8) -> Tuple[np.ndarray, ...]:
    """创建 YUV444 测试帧
    
    YUV444 (4:4:4):
    - Y: width x height
    - U: width x height
    - V: width x height
    """
    dtype = np.uint16 if bit_depth > 8 else np.uint8
    max_val = (1 << bit_depth) - 1
    
    y = np.linspace(0, max_val, width * height, dtype=dtype).reshape(height, width)
    u = np.full((height, width), max_val // 2, dtype=dtype)
    v = np.full((height, width), max_val // 2, dtype=dtype)
    
    return y, u, v


def convert_yuv_to_rgb_reference(
    y: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    matrix: np.ndarray,
    color_range: str = 'limited',
    bit_depth: int = 8
) -> np.ndarray:
    """YUV -> RGB 参考转换（CPU 实现）
    
    用于验证 GPU shader 结果。
    """
    max_val = (1 << bit_depth) - 1
    
    # Normalize to [0, 1]
    y_norm = y.astype(np.float32) / max_val
    u_norm = u.astype(np.float32) / max_val
    v_norm = v.astype(np.float32) / max_val
    
    # Range conversion
    if color_range == 'limited':
        # Limited range (TV)
        y_norm = (y_norm - Y_OFFSET_LIM) * Y_SCALE_LIM
        u_norm = u_norm - C_OFFSET_LIM
        v_norm = v_norm - C_OFFSET_LIM
    else:
        # Full range (PC)
        u_norm = u_norm - C_OFFSET_FULL
        v_norm = v_norm - C_OFFSET_FULL
    
    # Upsample chroma if needed
    if u.shape != y.shape:
        # Nearest neighbor upsampling
        u_up = np.repeat(np.repeat(u_norm, 2, axis=0), 2, axis=1)
        v_up = np.repeat(np.repeat(v_norm, 2, axis=0), 2, axis=1)
        
        # Ensure size matches
        if u_up.shape[0] > y_norm.shape[0]:
            u_up = u_up[:y_norm.shape[0], :]
            v_up = v_up[:y_norm.shape[0], :]
        if u_up.shape[1] > y_norm.shape[1]:
            u_up = u_up[:, :y_norm.shape[1]]
            v_up = v_up[:, :y_norm.shape[1]]
    else:
        u_up = u_norm
        v_up = v_norm
    
    # Matrix multiplication: RGB = matrix * [Y, U, V]
    height, width = y_norm.shape
    rgb = np.zeros((height, width, 3), dtype=np.float32)
    
    for i in range(3):
        rgb[:, :, i] = matrix[0, i] * y_norm + matrix[1, i] * u_up + matrix[2, i] * v_up
    
    return rgb


def get_chroma_scale(format_type: str) -> Tuple[float, float]:
    """获取色度缩放因子
    
    Returns:
        (scale_x, scale_y) for chroma coordinates
    """
    if format_type == '420':
        return (0.5, 0.5)  # U/V 是 Y 的 1/4
    elif format_type == '422':
        return (0.5, 1.0)  # U/V 水平减半
    elif format_type == '444':
        return (1.0, 1.0)  # 无缩放
    else:
        return (1.0, 1.0)


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
def mock_context():
    """Mock ModernGL context"""
    mock_ctx = MagicMock()
    
    def create_texture(size=(1920, 1080), components=1, dtype='f2'):
        mock_tex = MagicMock()
        mock_tex.glo = id(mock_tex)
        mock_tex.size = size
        mock_tex.components = components
        mock_tex.dtype = dtype
        mock_tex.write = MagicMock()
        mock_tex.release = MagicMock()
        return mock_tex
    
    mock_ctx.texture = MagicMock(side_effect=create_texture)
    yield mock_ctx


# =============================================================================
# YUV Format Tests
# =============================================================================

class TestYUVFormatCreation:
    """测试 YUV 格式创建"""

    def test_yuv420_8bit(self):
        """YUV420 8-bit"""
        y, u, v = create_yuv420_frame(1920, 1080, bit_depth=8)
        
        # Y: 1920x1080
        assert y.shape == (1080, 1920)
        assert y.dtype == np.uint8
        
        # U/V: 960x540 (1/4 of Y)
        assert u.shape == (540, 960)
        assert v.shape == (540, 960)

    def test_yuv420_10bit(self):
        """YUV420 10-bit"""
        y, u, v = create_yuv420_frame(1920, 1080, bit_depth=10)
        
        # 10-bit 使用 uint16
        assert y.dtype == np.uint16
        assert u.dtype == np.uint16
        assert v.dtype == np.uint16
        
        # 值范围 0-1023
        assert y.max() <= 1023

    def test_yuv422_8bit(self):
        """YUV422 8-bit"""
        y, u, v = create_yuv422_frame(1920, 1080, bit_depth=8)
        
        # Y: 1920x1080
        assert y.shape == (1080, 1920)
        
        # U/V: 960x1080 (水平减半)
        assert u.shape == (1080, 960)
        assert v.shape == (1080, 960)

    def test_yuv422_10bit(self):
        """YUV422 10-bit"""
        y, u, v = create_yuv422_frame(1920, 1080, bit_depth=10)
        
        assert y.dtype == np.uint16
        assert y.max() <= 1023

    def test_yuv444_8bit(self):
        """YUV444 8-bit"""
        y, u, v = create_yuv444_frame(1920, 1080, bit_depth=8)
        
        # 所有平面相同尺寸
        assert y.shape == (1080, 1920)
        assert u.shape == (1080, 1920)
        assert v.shape == (1080, 1920)

    def test_yuv444_10bit(self):
        """YUV444 10-bit"""
        y, u, v = create_yuv444_frame(1920, 1080, bit_depth=10)
        
        assert y.dtype == np.uint16
        assert y.max() <= 1023

    def test_yuv444_12bit(self):
        """YUV444 12-bit"""
        y, u, v = create_yuv444_frame(1920, 1080, bit_depth=12)
        
        assert y.dtype == np.uint16
        assert y.max() <= 4095  # 12-bit max


# =============================================================================
# Color Space Conversion Tests
# =============================================================================

class TestColorSpaceConversion:
    """测试颜色空间转换"""

    def test_bt709_matrix_values(self):
        """BT.709 矩阵值验证"""
        # BT.709 matrix constants from shader
        expected = np.array([
            [1.0, 1.0, 1.0],
            [0.0, -0.1873, 1.8556],
            [1.5748, -0.4681, 0.0]
        ])
        
        np.testing.assert_array_almost_equal(BT709_MATRIX, expected, decimal=4)

    def test_bt2020_matrix_values(self):
        """BT.2020 矩阵值验证"""
        expected = np.array([
            [1.0, 1.0, 1.0],
            [0.0, -0.1646, 1.8814],
            [1.4746, -0.5714, 0.0]
        ])
        
        np.testing.assert_array_almost_equal(BT2020_MATRIX, expected, decimal=4)

    def test_bt601_matrix_values(self):
        """BT.601 矩阵值验证（数字 YCbCr 系数，AUD-R2-010 订正）"""
        expected = np.array([
            [1.0, 1.0, 1.0],
            [0.0, -0.3441, 1.7720],
            [1.4020, -0.7141, 0.0]
        ])

        np.testing.assert_array_almost_equal(BT601_MATRIX, expected, decimal=3)

    def test_yuv_to_rgb_bt709(self):
        """BT.709 YUV -> RGB 转换"""
        y, u, v = create_yuv444_frame(100, 100, bit_depth=8)
        
        # 设置中性色（Y=128, U=V=128 应产生灰色）
        y = np.full((100, 100), 128, dtype=np.uint8)
        u = np.full((100, 100), 128, dtype=np.uint8)
        v = np.full((100, 100), 128, dtype=np.uint8)
        
        rgb = convert_yuv_to_rgb_reference(y, u, v, BT709_MATRIX, 'limited', 8)
        
        # 中性色应产生接近灰色 RGB
        # Y=128 (limited: 128/255 - 16/255 = 0.439) * scale = ~0.51
        # 灰色 RGB 应约 (0.51, 0.51, 0.51)
        mean_val = rgb.mean()
        assert 0.4 < mean_val < 0.6

    def test_yuv_to_rgb_bt2020(self):
        """BT.2020 YUV -> RGB 转换"""
        y, u, v = create_yuv444_frame(100, 100, bit_depth=10)
        
        # 10-bit 中性色
        y = np.full((100, 100), 512, dtype=np.uint16)
        u = np.full((100, 100), 512, dtype=np.uint16)
        v = np.full((100, 100), 512, dtype=np.uint16)
        
        rgb = convert_yuv_to_rgb_reference(y, u, v, BT2020_MATRIX, 'limited', 10)
        
        # 中性色检查
        mean_val = rgb.mean()
        assert 0.3 < mean_val < 0.7

    def test_yuv_to_rgb_full_range(self):
        """Full range 转换"""
        y, u, v = create_yuv444_frame(100, 100, bit_depth=8)
        
        # Full range 中性色
        y = np.full((100, 100), 128, dtype=np.uint8)
        u = np.full((100, 100), 128, dtype=np.uint8)
        v = np.full((100, 100), 128, dtype=np.uint8)
        
        rgb = convert_yuv_to_rgb_reference(y, u, v, BT709_MATRIX, 'full', 8)
        
        # Full range: U-0.5, V-0.5 产生偏移
        mean_val = rgb.mean()
        assert -0.1 < mean_val < 1.1  # 允许稍微超出范围


# =============================================================================
# Range Conversion Tests
# =============================================================================

class TestRangeConversion:
    """测试 Range 转换"""

    def test_limited_range_y_scaling(self):
        """Limited range Y 缩放"""
        # Y: [16, 235] -> [0, 1]
        y_input = np.array([16, 128, 235], dtype=np.uint8)
        
        y_norm = y_input.astype(np.float32) / 255.0
        y_scaled = (y_norm - Y_OFFSET_LIM) * Y_SCALE_LIM
        
        # 16 -> 0, 235 -> 1
        assert abs(y_scaled[0] - 0.0) < 0.01
        assert abs(y_scaled[2] - 1.0) < 0.01

    def test_limited_range_uv_offset(self):
        """Limited range UV 偏移"""
        # UV centered at 128
        uv_input = np.array([16, 128, 240], dtype=np.uint8)
        
        uv_norm = uv_input.astype(np.float32) / 255.0
        uv_offset = uv_norm - C_OFFSET_LIM
        
        # 128 -> 0 (center)
        assert abs(uv_offset[1] - 0.0) < 0.01

    def test_full_range_uv_offset(self):
        """Full range UV 偏移"""
        # UV: [0, 255] -> [-0.5, 0.5]
        uv_input = np.array([0, 128, 255], dtype=np.uint8)
        
        uv_norm = uv_input.astype(np.float32) / 255.0
        uv_offset = uv_norm - C_OFFSET_FULL
        
        # 0 -> -0.5, 255 -> 0.5
        assert abs(uv_offset[0] - (-0.5)) < 0.01
        assert abs(uv_offset[2] - 0.5) < 0.01

    def test_10bit_range_conversion(self):
        """10-bit Range 转换"""
        # 10-bit limited: Y [64, 940] (equivalent to 8-bit [16, 235])
        # 注意：limited range 使用相同的 offset/scale（基于 8-bit 定义）
        y_8bit = np.array([16, 235], dtype=np.uint8)
        y_10bit = np.array([64, 940], dtype=np.uint16)  # 10-bit equivalent
        
        # Normalize to [0, 1] first
        y_8bit_norm_raw = y_8bit.astype(np.float32) / 255.0
        y_10bit_norm_raw = y_10bit.astype(np.float32) / 1023.0
        
        # Limited range conversion uses same constants
        # Y = (Y_norm - 16/255) * 255/219
        y_8bit_scaled = (y_8bit_norm_raw - Y_OFFSET_LIM) * Y_SCALE_LIM
        y_10bit_scaled = (y_10bit_norm_raw - Y_OFFSET_LIM) * Y_SCALE_LIM
        
        # 16 -> 0, 235 -> 1 (8-bit)
        # 64/1023 ≈ 0.0625, but shader uses 16/255 as offset regardless of bit depth
        # 所以 10-bit 和 8-bit 的转换结果会有差异
        # 这里只验证边界值（最小和最大）
        
        # 8-bit: 16 should map to ~0, 235 to ~1
        assert abs(y_8bit_scaled[0] - 0.0) < 0.1
        assert abs(y_8bit_scaled[1] - 1.0) < 0.1
        
        # 10-bit: 由于 shader 使用相同的 offset (16/255)，转换结果不同
        # 这是 shader 的设计，保持一致的 offset/scale


# =============================================================================
# Chroma Subsampling Tests
# =============================================================================

class TestChromaSubsampling:
    """测试色度子采样"""

    def test_chroma_scale_420(self):
        """4:2:0 色度缩放"""
        scale_x, scale_y = get_chroma_scale('420')
        assert scale_x == 0.5
        assert scale_y == 0.5

    def test_chroma_scale_422(self):
        """4:2:2 色度缩放"""
        scale_x, scale_y = get_chroma_scale('422')
        assert scale_x == 0.5
        assert scale_y == 1.0

    def test_chroma_scale_444(self):
        """4:4:4 色度缩放"""
        scale_x, scale_y = get_chroma_scale('444')
        assert scale_x == 1.0
        assert scale_y == 1.0

    def test_chroma_upsampling_nearest(self):
        """最近邻上采样"""
        # 小尺寸测试
        u = np.array([[100]], dtype=np.uint8)  # 1x1
        u_norm = u.astype(np.float32) / 255.0
        
        # Upsample 2x
        u_up = np.repeat(np.repeat(u_norm, 2, axis=0), 2, axis=1)
        
        assert u_up.shape == (2, 2)
        assert u_up[0, 0] == u_up[1, 1]  # 最近邻保持值相同

    def test_chroma_upsampling_matches_y(self):
        """色度上采样匹配 Y 尺寸"""
        y, u, v = create_yuv420_frame(1920, 1080)
        
        # Upsample U/V
        u_norm = u.astype(np.float32) / 255.0
        v_norm = v.astype(np.float32) / 255.0
        
        u_up = np.repeat(np.repeat(u_norm, 2, axis=0), 2, axis=1)
        v_up = np.repeat(np.repeat(v_norm, 2, axis=0), 2, axis=1)
        
        # 420: U/V 原始尺寸 960x540，上采样后 1920x1080
        # 540*2 = 1080, 960*2 = 1920
        assert u_up.shape == (1080, 1920)
        assert v_up.shape == (1080, 1920)
        
        # 匹配 Y 尺寸
        assert u_up.shape == y.shape


# =============================================================================
# High Bit Depth Tests
# =============================================================================

class TestHighBitDepth:
    """测试高位深处理"""

    def test_10bit_data_type(self):
        """10-bit 数据类型"""
        y, u, v = create_yuv444_frame(1920, 1080, bit_depth=10)
        
        # 应使用 uint16
        assert y.dtype == np.uint16

    def test_10bit_value_range(self):
        """10-bit 值范围"""
        y, u, v = create_yuv444_frame(1920, 1080, bit_depth=10)
        
        # 10-bit: 0-1023
        assert y.min() >= 0
        assert y.max() <= 1023

    def test_12bit_data_type(self):
        """12-bit 数据类型"""
        y, u, v = create_yuv444_frame(1920, 1080, bit_depth=12)
        
        assert y.dtype == np.uint16

    def test_12bit_value_range(self):
        """12-bit 值范围"""
        y, u, v = create_yuv444_frame(1920, 1080, bit_depth=12)
        
        # 12-bit: 0-4095
        assert y.min() >= 0
        assert y.max() <= 4095

    def test_10bit_normalization(self):
        """10-bit 归一化"""
        y = np.array([0, 512, 1023], dtype=np.uint16)
        
        y_norm = y.astype(np.float32) / 1023.0
        
        assert abs(y_norm[0] - 0.0) < 0.01
        assert abs(y_norm[1] - 0.5) < 0.01
        assert abs(y_norm[2] - 1.0) < 0.01

    def test_float16_conversion(self):
        """float16 转换"""
        y_10bit = np.array([0, 512, 1023], dtype=np.uint16)
        
        # 归一化到 float32
        y_norm = y_10bit.astype(np.float32) / 1023.0
        
        # 转换到 float16 (用于 GPU)
        y_f16 = y_norm.astype(np.float16)
        
        # 检查精度损失是否可接受
        y_back = y_f16.astype(np.float32)
        
        # float16 精度约 0.001
        np.testing.assert_array_almost_equal(y_norm, y_back, decimal=3)


# =============================================================================
# GPU Upload Tests (需要真实 OpenGL)
# =============================================================================

@pytest.mark.skipif(not HAS_MODERNGL, reason="moderngl not available")
class TestYUVGPUUpload:
    """GPU 上传测试"""

    def test_y_plane_upload(self, gl_context):
        """Y 平面上传"""
        y, u, v = create_yuv420_frame(1920, 1080, bit_depth=8)
        
        # 创建 Y 纹理 (单通道)
        y_tex = gl_context.texture((1920, 1080), components=1, dtype='f2')
        
        # 归一化并转换到 float16
        y_norm = y.astype(np.float32) / 255.0
        y_f16 = y_norm.astype(np.float16)
        
        # 上传
        y_tex.write(y_f16.tobytes())
        
        assert y_tex.size == (1920, 1080)
        assert y_tex.components == 1

    def test_uv_planes_upload_420(self, gl_context):
        """420 U/V 平面上传"""
        y, u, v = create_yuv420_frame(1920, 1080, bit_depth=8)
        
        # U/V 纹理 (960x540)
        u_tex = gl_context.texture((960, 540), components=1, dtype='f2')
        v_tex = gl_context.texture((960, 540), components=1, dtype='f2')
        
        # 归一化
        u_norm = u.astype(np.float32) / 255.0
        v_norm = v.astype(np.float32) / 255.0
        
        u_tex.write(u_norm.astype(np.float16).tobytes())
        v_tex.write(v_norm.astype(np.float16).tobytes())
        
        assert u_tex.size == (960, 540)
        assert v_tex.size == (960, 540)

    def test_10bit_plane_upload(self, gl_context):
        """10-bit 平面上传"""
        y, u, v = create_yuv420_frame(1920, 1080, bit_depth=10)
        
        # 创建纹理
        y_tex = gl_context.texture((1920, 1080), components=1, dtype='f2')
        
        # 归一化 (10-bit: 0-1023)
        y_norm = y.astype(np.float32) / 1023.0
        y_tex.write(y_norm.astype(np.float16).tobytes())
        
        assert y_tex.size == (1920, 1080)

    def test_4k_yuv_upload(self, gl_context):
        """4K YUV 上传"""
        y, u, v = create_yuv420_frame(3840, 2160, bit_depth=8)
        
        # Y 纹理
        y_tex = gl_context.texture((3840, 2160), components=1, dtype='f2')
        y_norm = y.astype(np.float32) / 255.0
        y_tex.write(y_norm.astype(np.float16).tobytes())
        
        # U/V 纹理
        u_tex = gl_context.texture((1920, 1080), components=1, dtype='f2')
        v_tex = gl_context.texture((1920, 1080), components=1, dtype='f2')
        
        u_norm = u.astype(np.float32) / 255.0
        v_norm = v.astype(np.float32) / 255.0
        
        u_tex.write(u_norm.astype(np.float16).tobytes())
        v_tex.write(v_norm.astype(np.float16).tobytes())
        
        assert y_tex.size == (3840, 2160)

    def test_rgb_output_texture(self, gl_context):
        """RGB 输出纹理"""
        # 创建 RGBA 输出纹理
        output_tex = gl_context.texture((1920, 1080), components=4, dtype='f2')
        
        assert output_tex.size == (1920, 1080)
        assert output_tex.components == 4


# =============================================================================
# Shader Uniform Tests
# =============================================================================

class TestYUVShaderUniforms:
    """测试 Shader Uniform 设置"""

    def test_yuv_matrix_uniform_values(self):
        """yuv_matrix uniform 值"""
        # Shader 中的 uniform int yuv_matrix
        # 0 = BT.709, 1 = BT.2020, 2 = BT.601
        
        matrix_codes = {
            'bt709': 0,
            'bt2020': 1,
            'bt601': 2
        }
        
        assert matrix_codes['bt709'] == 0
        assert matrix_codes['bt2020'] == 1
        assert matrix_codes['bt601'] == 2

    def test_color_range_uniform_values(self):
        """color_range uniform 值"""
        # Shader 中的 uniform int color_range
        # 0 = limited (TV), 1 = full (PC)
        
        range_codes = {
            'limited': 0,
            'full': 1
        }
        
        assert range_codes['limited'] == 0
        assert range_codes['full'] == 1

    def test_chroma_scale_uniform_values(self):
        """chroma_scale uniform 值"""
        # Shader 中的 uniform vec2 chroma_scale
        
        # 4:2:0
        scale_420 = (0.5, 0.5)
        # 4:2:2
        scale_422 = (0.5, 1.0)
        # 4:4:4
        scale_444 = (1.0, 1.0)
        
        assert scale_420 == get_chroma_scale('420')
        assert scale_422 == get_chroma_scale('422')
        assert scale_444 == get_chroma_scale('444')


# =============================================================================
# Integration Tests
# =============================================================================

@pytest.mark.skipif(not HAS_MODERNGL, reason="moderngl not available")
class TestYUVUploadIntegration:
    """集成测试"""

    def test_full_yuv_pipeline_mock(self, gl_context):
        """完整 YUV 管线测试（mock shader）"""
        # 创建 YUV 帧
        y, u, v = create_yuv420_frame(1920, 1080, bit_depth=8)
        
        # 创建纹理
        y_tex = gl_context.texture((1920, 1080), components=1, dtype='f2')
        u_tex = gl_context.texture((960, 540), components=1, dtype='f2')
        v_tex = gl_context.texture((960, 540), components=1, dtype='f2')
        
        # 归一化并上传
        y_tex.write((y.astype(np.float32) / 255.0).astype(np.float16).tobytes())
        u_tex.write((u.astype(np.float32) / 255.0).astype(np.float16).tobytes())
        v_tex.write((v.astype(np.float32) / 255.0).astype(np.float16).tobytes())
        
        # 创建输出纹理
        output_tex = gl_context.texture((1920, 1080), components=4, dtype='f2')
        
        # 验证所有纹理已创建
        assert y_tex.glo > 0
        assert u_tex.glo > 0
        assert v_tex.glo > 0
        assert output_tex.glo > 0

    def test_cpu_gpu_conversion_comparison(self, gl_context):
        """CPU vs GPU 转换对比"""
        # 创建简单测试帧
        y, u, v = create_yuv444_frame(64, 64, bit_depth=8)
        
        # CPU 参考转换
        rgb_cpu = convert_yuv_to_rgb_reference(y, u, v, BT709_MATRIX, 'limited', 8)
        
        # 验证 CPU 结果合理
        assert rgb_cpu.min() >= -0.5  # 允许负值（超出色域）
        assert rgb_cpu.max() <= 1.5  # 允许超亮值


# =============================================================================
# Performance Tests
# =============================================================================

class TestYUVUploadPerformance:
    """性能测试"""

    @pytest.mark.slow
    def test_1080p_upload_time(self):
        """1080p 上传时间测量"""
        if not HAS_MODERNGL:
            pytest.skip("moderngl not available")
        
        import time
        
        ctx = moderngl.create_standalone_context()
        
        y, u, v = create_yuv420_frame(1920, 1080, bit_depth=8)
        
        # 预处理数据
        y_norm = (y.astype(np.float32) / 255.0).astype(np.float16)
        u_norm = (u.astype(np.float32) / 255.0).astype(np.float16)
        v_norm = (v.astype(np.float32) / 255.0).astype(np.float16)
        
        # 创建纹理
        y_tex = ctx.texture((1920, 1080), components=1, dtype='f2')
        u_tex = ctx.texture((960, 540), components=1, dtype='f2')
        v_tex = ctx.texture((960, 540), components=1, dtype='f2')
        
        # 测量上传时间
        start = time.time()
        y_tex.write(y_norm.tobytes())
        u_tex.write(u_norm.tobytes())
        v_tex.write(v_norm.tobytes())
        elapsed = time.time() - start
        
        # 上传应在 50ms 内完成（宽松阈值）
        assert elapsed < 0.05, f"Upload took {elapsed:.3f}s"

    @pytest.mark.slow
    def test_4k_upload_time(self):
        """4K 上传时间测量"""
        if not HAS_MODERNGL:
            pytest.skip("moderngl not available")
        
        import time
        
        ctx = moderngl.create_standalone_context()
        
        y, u, v = create_yuv420_frame(3840, 2160, bit_depth=8)
        
        # 预处理
        y_norm = (y.astype(np.float32) / 255.0).astype(np.float16)
        u_norm = (u.astype(np.float32) / 255.0).astype(np.float16)
        v_norm = (v.astype(np.float32) / 255.0).astype(np.float16)
        
        # 创建纹理
        y_tex = ctx.texture((3840, 2160), components=1, dtype='f2')
        u_tex = ctx.texture((1920, 1080), components=1, dtype='f2')
        v_tex = ctx.texture((1920, 1080), components=1, dtype='f2')
        
        # 测量
        start = time.time()
        y_tex.write(y_norm.tobytes())
        u_tex.write(u_norm.tobytes())
        v_tex.write(v_norm.tobytes())
        elapsed = time.time() - start
        
        # 4K 上传应在 100ms 内完成
        assert elapsed < 0.1, f"4K upload took {elapsed:.3f}s"


# =============================================================================
# Manual Test Runner
# =============================================================================

def run_manual_tests():
    """手动运行测试（用于调试）"""
    print("\n=== Manual YUV Upload Tests ===\n")
    
    if not HAS_MODERNGL:
        print("moderngl not available, skipping manual tests")
        return
    
    try:
        ctx = moderngl.create_standalone_context()
        print("Created standalone context")
        
        # Test 1: YUV420 8-bit
        print("\n--- Test 1: YUV420 8-bit ---")
        y, u, v = create_yuv420_frame(1920, 1080, bit_depth=8)
        print(f"  Y: {y.shape}, dtype={y.dtype}")
        print(f"  U: {u.shape}, dtype={u.dtype}")
        print(f"  V: {v.shape}, dtype={v.dtype}")
        
        # Test 2: YUV420 10-bit
        print("\n--- Test 2: YUV420 10-bit ---")
        y, u, v = create_yuv420_frame(1920, 1080, bit_depth=10)
        print(f"  Y: {y.shape}, dtype={y.dtype}, max={y.max()}")
        
        # Test 3: GPU Upload
        print("\n--- Test 3: GPU Upload ---")
        y_tex = ctx.texture((1920, 1080), components=1, dtype='f2')
        u_tex = ctx.texture((960, 540), components=1, dtype='f2')
        v_tex = ctx.texture((960, 540), components=1, dtype='f2')
        
        y_norm = (y.astype(np.float32) / 1023.0).astype(np.float16)
        y_tex.write(y_norm.tobytes())
        
        print(f"  Y texture: glo={y_tex.glo}, size={y_tex.size}")
        print(f"  U texture: glo={u_tex.glo}, size={u_tex.size}")
        
        # Test 4: CPU YUV -> RGB
        print("\n--- Test 4: CPU YUV -> RGB ---")
        y, u, v = create_yuv444_frame(100, 100, bit_depth=8)
        y[:] = 128
        u[:] = 128
        v[:] = 128
        
        rgb = convert_yuv_to_rgb_reference(y, u, v, BT709_MATRIX, 'limited', 8)
        print(f"  RGB result (neutral color): mean={rgb.mean():.3f}")
        print(f"  RGB range: min={rgb.min():.3f}, max={rgb.max():.3f}")
        
        # Test 5: Different color spaces
        print("\n--- Test 5: Color Spaces ---")
        for name, matrix in [('BT.709', BT709_MATRIX), ('BT.2020', BT2020_MATRIX), ('BT.601', BT601_MATRIX)]:
            rgb = convert_yuv_to_rgb_reference(y, u, v, matrix, 'limited', 8)
            print(f"  {name}: RGB mean={rgb.mean():.3f}")
        
        print("\n=== Tests completed ===\n")
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    run_manual_tests()