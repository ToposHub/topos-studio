"""
Video Upload Module - PBO 异步上传和 YUV 平面直接上传

设计原则：
- OpenGL/ModernGL 操作只在渲染线程执行（遵守 .clinerules）
- 资源显式 release
- 完整类型注解

架构说明：
- PBOUploader: Pixel Buffer Object 异步上传预研
- YuvPlaneUploader: YUV 平面直接上传到 GPU
- 性能对比测试方法

使用场景：
- 高性能视频帧上传（减少 CPU/GPU 同步）
- 支持 YUV planar 格式（yuv420p10le, yuv422p10le 等）
- 避免 CPU 端 YUV -> RGB 转换

注意事项：
- PBO 需要在 OpenGL 上下文激活时使用
- ModernGL 的 Buffer 可以用于 PBO，但需要特殊配置
- 实际性能提升取决于 GPU 驱动实现
"""

from __future__ import annotations

import time
import logging
import numpy as np
import moderngl
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

from src.shared.media.frame import DecodedFrame, get_pixel_format

logger = logging.getLogger(__name__)

# 外部 YUV shader 相对仓库根的位置。
# AUD-39712: 此前 create_yuv_program 用 CWD 相对路径 'src/features/...'
# 加载外部 shader——启动目录不是仓库根时 FileNotFoundError 被静默吞掉，
# 无声降级到内置 shader（颜色行为随启动目录漂移）。改为以 __file__
# 上溯锚定仓库根（src/shared/video/video_upload.py → parents[3]）。
_VIDEO_YUV_SHADER_RELATIVE = Path('src') / 'features' / 'color' / 'shaders' / 'video_yuv.glsl'


def _video_yuv_shader_candidates() -> List[Path]:
    """返回外部 YUV shader 的候选路径（仓库根锚定优先，CWD 兜底）。

    - 首选：<repo_root>/src/features/color/shaders/video_yuv.glsl
      （由本文件位置推导，与进程启动目录无关）
    - 次选：CWD 相对路径（旧行为兼容，如冻结部署等特殊布局）
    """
    repo_root = Path(__file__).resolve().parents[3]
    return [repo_root / _VIDEO_YUV_SHADER_RELATIVE, _VIDEO_YUV_SHADER_RELATIVE]


# ================================================================
# P4.3: PBO 异步上传预研
# ================================================================

class PBOUploader:
    """
    Pixel Buffer Object 异步上传器（已修复）。

    ⚠️ 此类保留用于向后兼容。新代码请使用 yuv_gpu_upload.py 中的 PBOUploadPool。

    修复说明：
    - 旧版本使用 buffer.read() 回读到 CPU 再上传（错误！）
    - 新版本正确使用 texture.write(pbo_buffer) 实现 GPU 侧 DMA 传输
    - ModernGL 的 texture.write(Buffer) 内部绑定 GL_PIXEL_UNPACK_BUFFER，
      等同于原生 glTexSubImage2D + PBO 的标准异步上传路径。
    """

    def __init__(self, ctx: moderngl.Context, buffer_count: int = 2) -> None:
        """
        初始化 PBO 上传器。

        Args:
            ctx: ModernGL 上下文（必须在渲染线程）
            buffer_count: PBO 数量（双缓冲=2，三缓冲=3）
        """
        self._ctx = ctx
        self._buffer_count = max(1, int(buffer_count))
        self._buffers: List[moderngl.Buffer] = []
        self._buffer_sizes: List[int] = []
        self._current_index: int = 0
        self._initialized: bool = False

        # 性能统计
        self._upload_times: List[float] = []
        self._direct_times: List[float] = []

    def initialize(self, max_frame_size: int) -> None:
        """
        预分配 PBO 缓冲。

        Args:
            max_frame_size: 最大帧数据大小（字节）
            例如：4K RGBA16 = 3840*2160*4*2 = 66,355,200 bytes
        """
        if self._initialized:
            self._release_buffers()

        for i in range(self._buffer_count):
            try:
                buffer = self._ctx.buffer(reserve=max_frame_size)
            except Exception as exc:
                logger.warning("PBOUploader initialize skipped: %s", exc)
                self._release_buffers()
                self._initialized = True
                return
            self._buffers.append(buffer)
            self._buffer_sizes.append(max_frame_size)

        self._initialized = True
        logger.info(f"PBOUploader initialized with {self._buffer_count} buffers, "
                    f"size={max_frame_size} bytes")

    def upload_async(self, data: np.ndarray, texture: moderngl.Texture) -> float:
        """
        异步上传数据到纹理（已修复）。

        正确的 PBO 上传流程：
        1. CPU 写入数据到 PBO 缓冲
        2. texture.write(pbo) 触发 GPU 侧 PBO → 纹理 DMA 传输

        Args:
            data: numpy 数组（uint8 或 float32）
            texture: 目标纹理

        Returns:
            上传耗时（毫秒）
        """
        if not self._initialized:
            raise RuntimeError("PBOUploader not initialized")
        if not self._buffers:
            logger.debug("PBOUploader unavailable; using direct upload fallback")
            return self.upload_direct(data, texture)

        start_time = time.perf_counter()

        # 确保 numpy 数组连续
        contiguous = np.ascontiguousarray(data)
        data_size = contiguous.nbytes

        # 获取当前 PBO
        current_buffer = self._buffers[self._current_index]

        # 检查缓冲大小
        if data_size > self._buffer_sizes[self._current_index]:
            current_buffer.release()
            current_buffer = self._ctx.buffer(reserve=data_size)
            self._buffers[self._current_index] = current_buffer
            self._buffer_sizes[self._current_index] = data_size

        # 步骤 1: orphan() 通知驱动可以丢弃旧数据，避免 GPU stall
        current_buffer.orphan()

        # 步骤 2: CPU → PBO（通过 Python buffer protocol，零拷贝）
        current_buffer.write(contiguous)

        # 步骤 3: PBO → texture（GPU 侧 DMA，异步）
        # ModernGL 内部绑定 GL_PIXEL_UNPACK_BUFFER 后调用 glTexSubImage2D
        texture.write(current_buffer)

        # 切换到下一个 PBO（双缓冲旋转）
        self._current_index = (self._current_index + 1) % self._buffer_count

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        self._upload_times.append(elapsed_ms)

        return elapsed_ms

    def upload_direct(self, data: np.ndarray, texture: moderngl.Texture) -> float:
        """
        直接上传（对比基准）。

        Args:
            data: numpy 数组
            texture: 目标纹理

        Returns:
            上传耗时（毫秒）
        """
        start_time = time.perf_counter()

        # 直接写入纹理
        texture.write(data.tobytes())

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        self._direct_times.append(elapsed_ms)

        return elapsed_ms

    def get_performance_stats(self) -> Dict[str, Any]:
        """
        获取性能统计。

        Returns:
            包含 PBO 和直接上传统计的字典
        """
        stats = {
            'pbo_upload': {
                'count': len(self._upload_times),
                'avg_ms': np.mean(self._upload_times) if self._upload_times else 0.0,
                'min_ms': np.min(self._upload_times) if self._upload_times else 0.0,
                'max_ms': np.max(self._upload_times) if self._upload_times else 0.0,
            },
            'direct_upload': {
                'count': len(self._direct_times),
                'avg_ms': np.mean(self._direct_times) if self._direct_times else 0.0,
                'min_ms': np.min(self._direct_times) if self._direct_times else 0.0,
                'max_ms': np.max(self._direct_times) if self._direct_times else 0.0,
            },
            'improvement_ratio': 0.0,
        }

        # 计算提升比例
        if stats['pbo_upload']['avg_ms'] > 0 and stats['direct_upload']['avg_ms'] > 0:
            pbo_avg = stats['pbo_upload']['avg_ms']
            direct_avg = stats['direct_upload']['avg_ms']
            stats['improvement_ratio'] = (direct_avg - pbo_avg) / direct_avg

        return stats

    def get_upload_time_estimate(self, data_size: int) -> float:
        """
        估算上传时间。

        基于 PCIe 3.0 带宽（约 16 GB/s）的理论值。

        Args:
            data_size: 数据大小（字节）

        Returns:
            估算时间（毫秒）
        """
        # PCIe 3.0 x16 带宽约 16 GB/s
        # 实际有效带宽约 12-14 GB/s
        effective_bandwidth_gbps = 12.0

        # 理论传输时间
        theoretical_ms = data_size / (effective_bandwidth_gbps * 1e6)

        # 实际考虑驱动开销，增加 50% 缓冲
        estimated_ms = theoretical_ms * 1.5

        return estimated_ms

    def release(self) -> None:
        """释放所有 PBO 缓冲"""
        self._release_buffers()
        self._initialized = False

    def _release_buffers(self) -> None:
        """释放缓冲列表"""
        for buffer in self._buffers:
            try:
                buffer.release()
            except Exception as e:
                logger.warning(f"Failed to release PBO buffer: {e}")
        self._buffers.clear()
        self._buffer_sizes.clear()
        self._current_index = 0


# ================================================================
# P4.4: YUV 平面直接上传
# ================================================================

class YuvPlaneUploader:
    """
    YUV 平面直接上传到 GPU。

    对于 YUV planar 格式（如 yuv420p10le, yuv422p10le）：
    - Y plane: R16 texture (width, height)
    - U plane: R16 texture (width/2, height/2) for 420, or (width/2, height) for 422
    - V plane: R16 texture (width/2, height/2) for 420, or (width/2, height) for 422

    shader 中组合并转换为 RGB。

    优势：
    - 避免 CPU 端 YUV -> RGB 转换（节省大量计算）
    - 保持 10-bit 精度（不经 8-bit 降级）
    - GPU 端 YUV 转换更高效（并行处理）

    支持格式：
    - yuv420p: 8-bit 4:2:0
    - yuv422p: 8-bit 4:2:2
    - yuv420p10le: 10-bit 4:2:0
    - yuv422p10le: 10-bit 4:2:2
    - yuv444p10le: 10-bit 4:4:4
    """

    # YUV 到 RGB 转换矩阵
    # BT.709 (SDR, Rec. 709)
    BT709_MATRIX = np.array([
        [1.0, 1.0, 1.0],
        [0.0, -0.1873, 1.8556],
        [1.5748, -0.4681, 0.0]
    ], dtype=np.float32)

    # BT.2020 (HDR, Rec. 2020)
    BT2020_MATRIX = np.array([
        [1.0, 1.0, 1.0],
        [0.0, -0.1646, 1.8814],
        [1.4746, -0.5714, 0.0]
    ], dtype=np.float32)

    # BT.601 (旧标准，PAL/NTSC)
    BT601_MATRIX = np.array([
        [1.0, 1.0, 1.0],
        [0.0, -0.3945, 2.032],
        [1.140, -0.581, 0.0]
    ], dtype=np.float32)

    def __init__(self, ctx: moderngl.Context) -> None:
        """
        初始化 YUV 平面上传器。

        Args:
            ctx: ModernGL 上下文（必须在渲染线程）
        """
        self._ctx = ctx
        self._textures: Dict[str, moderngl.Texture] = {}
        self._program: Optional[moderngl.Program] = None

        # 默认参数
        self._yuv_matrix: str = 'bt709'
        self._color_range: str = 'limited'  # 'limited' or 'full'

    def upload_planes(self, frame: DecodedFrame) -> Tuple[moderngl.Texture, ...]:
        """
        上传 YUV 平面。

        Args:
            frame: 解码后的帧（必须包含 planes 数据）

        Returns:
            (y_texture, u_texture, v_texture) 元组

        Raises:
            ValueError: 帧不包含平面数据
        """
        if not frame.is_planar or not frame.planes:
            raise ValueError(f"Frame must have planar data, got {frame.pixel_format}")

        format_info = frame.format_info
        if format_info is None:
            format_info = get_pixel_format(frame.pixel_format)
            if format_info is None:
                raise ValueError(f"Unknown pixel format: {frame.pixel_format}")

        # 释放旧纹理
        self._release_textures()

        # 获取平面数据
        y_plane = frame.planes[0]
        u_plane = frame.planes[1]
        v_plane = frame.planes[2]

        # 计算纹理尺寸
        y_width, y_height = y_plane.shape[1], y_plane.shape[0]

        # 根据子采样计算色度平面尺寸
        u_width = u_plane.shape[1]
        u_height = u_plane.shape[0]
        v_width = v_plane.shape[1]
        v_height = v_plane.shape[0]

        # 确定纹理格式（基于位深度）
        if format_info.bit_depth <= 8:
            tex_dtype = 'f1'  # uint8 -> R8（硬件采样自动 /255 归一化）
            # AUD-39710: R8 纹理按每像素 1 字节解释。此前把 float32 数据
            # （4 字节/像素）写入 f1 纹理，write() 只消费前 1/4 像素的
            # 位模式碎片、GL 端按 UNSIGNED_BYTE 解释为乱码——8-bit
            # planar 上传结果必然错误。改为直接上传原始 uint8 平面：
            # 归一化交给硬件（R8 采样即得 [0,1]），字节与像素格式严格
            # 匹配（w*h 字节 == w*h 像素 * 1 字节）。
            y_data = np.ascontiguousarray(y_plane, dtype=np.uint8)
            u_data = np.ascontiguousarray(u_plane, dtype=np.uint8)
            v_data = np.ascontiguousarray(v_plane, dtype=np.uint8)
        elif format_info.bit_depth <= 16:
            tex_dtype = 'f2'  # float16 -> R16F
            # 10-bit 数据存储在 uint16 中，归一化到 [0, 1]
            max_val = (1 << format_info.bit_depth) - 1
            y_data = y_plane.astype(np.float32) / max_val
            u_data = u_plane.astype(np.float32) / max_val
            v_data = v_plane.astype(np.float32) / max_val
            # 转换为 float16 以节省带宽
            y_data = y_data.astype(np.float16)
            u_data = u_data.astype(np.float16)
            v_data = v_data.astype(np.float16)
        else:
            tex_dtype = 'f4'  # float32 -> R32F
            max_val = (1 << format_info.bit_depth) - 1
            y_data = y_plane.astype(np.float32) / max_val
            u_data = u_plane.astype(np.float32) / max_val
            v_data = v_plane.astype(np.float32) / max_val

        # 创建纹理
        # AUD-39715: 每创建一张立即登记进 _textures。若后续平面的创建/
        # 写入抛 GL 错误，已建纹理仍在 _textures 中，可被下次
        # _release_textures() 正常释放，不再滞留局部变量泄漏 GPU 内存。
        y_tex = self._ctx.texture((y_width, y_height), 1, dtype=tex_dtype)
        self._textures['y'] = y_tex
        y_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        y_tex.write(y_data.tobytes())

        u_tex = self._ctx.texture((u_width, u_height), 1, dtype=tex_dtype)
        self._textures['u'] = u_tex
        u_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        u_tex.write(u_data.tobytes())

        v_tex = self._ctx.texture((v_width, v_height), 1, dtype=tex_dtype)
        self._textures['v'] = v_tex
        v_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        v_tex.write(v_data.tobytes())

        logger.info(f"Uploaded YUV planes: Y={y_width}x{y_height}, "
                    f"U={u_width}x{u_height}, V={v_width}x{v_height}, "
                    f"dtype={tex_dtype}")

        return (y_tex, u_tex, v_tex)

    def create_yuv_program(self) -> moderngl.Program:
        """
        创建 YUV 转 RGB shader。

        Returns:
            ModernGL Program 对象
        """
        # AUD-39712: 路径由 _video_yuv_shader_candidates() 锚定仓库根，
        # 不再依赖 CWD；所有候选都不存在时才回退内置 shader。
        shader_path: Optional[Path] = None
        for candidate in _video_yuv_shader_candidates():
            if candidate.is_file():
                shader_path = candidate
                break

        if shader_path is None:
            logger.warning(
                "YUV shader file not found (tried: %s), using built-in shader",
                ', '.join(str(p) for p in _video_yuv_shader_candidates()),
            )
            return self._create_builtin_yuv_program()

        try:
            with open(shader_path, 'r') as f:
                shader_source = f.read()

            # 分离 vertex 和 fragment shader
            # shader 文件格式：VS 在前，FS 在后（以 // FRAGMENT SHADER 分隔）
            parts = shader_source.split('// FRAGMENT SHADER')
            if len(parts) == 2:
                vs_source = parts[0].strip()
                fs_source = parts[1].strip()
            else:
                # 如果没有分隔符，使用默认 VS
                vs_source = self._get_default_vertex_shader()
                fs_source = shader_source

            self._program = self._ctx.program(
                vertex_shader=vs_source,
                fragment_shader=fs_source
            )

            logger.info("YUV shader program created successfully from %s", shader_path)
            return self._program

        except (OSError, ValueError, moderngl.Error) as e:
            logger.error(f"Failed to create YUV shader: {e}")
            return self._create_builtin_yuv_program()

    def _create_builtin_yuv_program(self) -> moderngl.Program:
        """创建内置 YUV shader（备用）"""
        vs = self._get_default_vertex_shader()

        fs = """
        #version 330

        uniform sampler2D y_plane;
        uniform sampler2D u_plane;
        uniform sampler2D v_plane;
        uniform int yuv_matrix;  // 0=BT.709, 1=BT.2020, 2=BT.601
        uniform int color_range; // 0=limited, 1=full
        uniform vec2 chroma_uv_scale;  // Chroma UV coordinate scale (default 1.0, 1.0 for independent textures)
        uniform vec2 chroma_uv_offset; // Chroma UV coordinate offset (default 0.0, 0.0)

        in vec2 v_texcoord;
        out vec4 f_color;

        // BT.709 matrix (limited range, Y=[16,235], UV=[16,240])
        const mat3 bt709 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.1873, 1.8556,
            1.5748, -0.4681, 0.0
        );

        // BT.2020 matrix
        const mat3 bt2020 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.1646, 1.8814,
            1.4746, -0.5714, 0.0
        );

        // BT.601 matrix
        const mat3 bt601 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.3945, 2.032,
            1.140, -0.581, 0.0
        );

        void main() {
            // 读取 Y 平面（全分辨率）
            float y = texture(y_plane, v_texcoord).r;

            // 读取 U/V 平面（独立纹理，纹理坐标已归一化）
            // 对于 YUV420/YUV422/YUV444 的独立 U/V 纹理，纹理坐标范围本身就是 [0, 1]
            vec2 chroma_uv = v_texcoord * chroma_uv_scale + chroma_uv_offset;
            float u = texture(u_plane, chroma_uv).r;
            float v = texture(v_plane, chroma_uv).r;

            // Limited range offset
            // Y: [16,235] -> [0,1],  offset = 16/255, scale = 255/219
            // C: [16,240] -> [-0.5,0.5], offset = 128/255, scale = 255/224
            if (color_range == 0) {
                y = (y - 16.0 / 255.0) * 255.0 / 219.0;
                u = (u - 128.0 / 255.0) * (255.0 / 224.0);
                v = (v - 128.0 / 255.0) * (255.0 / 224.0);
            } else {
                // Full range: no offset
                u = u - 0.5;
                v = v - 0.5;
            }

            // 选择转换矩阵
            mat3 matrix = bt709;
            if (yuv_matrix == 1) {
                matrix = bt2020;
            } else if (yuv_matrix == 2) {
                matrix = bt601;
            }

            // YUV -> RGB
            vec3 rgb = matrix * vec3(y, u, v);

            // 输出（保持 HDR 精度，不做 clamp）
            f_color = vec4(rgb, 1.0);
        }
        """

        self._program = self._ctx.program(vertex_shader=vs, fragment_shader=fs)
        return self._program

    def _get_default_vertex_shader(self) -> str:
        """获取默认顶点着色器"""
        return """
        #version 330

        in vec2 in_vert;
        in vec2 in_texcoord;
        out vec2 v_texcoord;

        void main() {
            gl_Position = vec4(in_vert, 0.0, 1.0);
            v_texcoord = in_texcoord;
        }
        """

    def set_yuv_matrix(self, matrix: str) -> None:
        """
        设置 YUV 转换矩阵。

        Args:
            matrix: 'bt709', 'bt2020', 或 'bt601'
        """
        if matrix in ('bt709', 'bt2020', 'bt601'):
            self._yuv_matrix = matrix
        else:
            logger.warning(f"Unknown YUV matrix: {matrix}, using bt709")
            self._yuv_matrix = 'bt709'

    def set_color_range(self, range_type: str) -> None:
        """
        设置颜色范围。

        Args:
            range_type: 'limited' (TV range) 或 'full' (PC range)
        """
        if range_type in ('limited', 'full'):
            self._color_range = range_type
        else:
            logger.warning(f"Unknown color range: {range_type}, using limited")
            self._color_range = 'limited'

    def release(self) -> None:
        """释放所有纹理和程序"""
        self._release_textures()

        if self._program:
            try:
                self._program.release()
            except Exception as e:
                logger.warning(f"Failed to release YUV program: {e}")
            self._program = None

    def _release_textures(self) -> None:
        """释放所有纹理"""
        for name, tex in self._textures.items():
            try:
                tex.release()
            except Exception as e:
                logger.warning(f"Failed to release {name} texture: {e}")
        self._textures.clear()


# ================================================================
# 性能测试工具
# ================================================================

class UploadPerformanceTester:
    """
    上传性能测试工具。

    用于对比：
    - 直接 texture.write() vs PBO upload
    - YUV 平面上传 vs RGB 上传
    - 不同分辨率（4K, 1080p, 720p）
    """

    def __init__(self, ctx: moderngl.Context) -> None:
        """
        初始化测试器。

        Args:
            ctx: ModernGL 上下文
        """
        self._ctx = ctx
        self._pbo_uploader = PBOUploader(ctx, buffer_count=2)
        self._yuv_uploader = YuvPlaneUploader(ctx)
        self._results: Dict[str, Any] = {}

    def test_direct_vs_pbo(self, resolutions: List[Tuple[int, int]]) -> Dict[str, Any]:
        """
        测试直接上传 vs PBO 上传。

        Args:
            resolutions: 分辨率列表 [(width, height), ...]

        Returns:
            测试结果字典
        """
        results = {}

        for w, h in resolutions:
            # 创建测试数据（RGBA float32）
            data = np.random.rand(h, w, 4).astype(np.float32)
            data_size = data.nbytes

            # 初始化 PBO
            self._pbo_uploader.initialize(data_size * 2)  # 双缓冲需要足够空间

            # 创建纹理
            tex = self._ctx.texture((w, h), 4, dtype='f4')

            # 测试直接上传
            for _ in range(10):  # 预热
                self._pbo_uploader.upload_direct(data, tex)

            # 测试 PBO 上传
            for _ in range(10):  # 预热
                self._pbo_uploader.upload_async(data, tex)

            # 正式测试
            for _ in range(100):
                self._pbo_uploader.upload_direct(data, tex)
                self._pbo_uploader.upload_async(data, tex)

            # 获取统计
            stats = self._pbo_uploader.get_performance_stats()

            key = f"{w}x{h}"
            results[key] = {
                'resolution': (w, h),
                'data_size_mb': data_size / 1e6,
                'direct_avg_ms': stats['direct_upload']['avg_ms'],
                'pbo_avg_ms': stats['pbo_upload']['avg_ms'],
                'improvement_ratio': stats['improvement_ratio'],
            }

            # 清理
            tex.release()
            self._pbo_uploader.release()

        self._results['pbo_vs_direct'] = results
        return {**results, 'pbo_vs_direct': results}

    def test_yuv_vs_rgb_upload(self, resolutions: List[Tuple[int, int]]) -> Dict[str, Any]:
        """
        测试 YUV 平面上传 vs RGB 上传。

        Args:
            resolutions: 分辨率列表

        Returns:
            测试结果字典
        """
        results = {}

        for w, h in resolutions:
            # 模拟 YUV420p10le 数据
            y_plane = np.random.randint(0, 1024, size=(h, w), dtype=np.uint16)
            u_plane = np.random.randint(0, 1024, size=(h//2, w//2), dtype=np.uint16)
            v_plane = np.random.randint(0, 1024, size=(h//2, w//2), dtype=np.uint16)

            # 模拟 RGB 上传（转换后）
            rgb_data = np.random.rand(h, w, 3).astype(np.float32)

            # 测试 YUV 平面上传
            start_yuv = time.perf_counter()
            y_tex = self._ctx.texture((w, h), 1, dtype='f2')
            u_tex = self._ctx.texture((w//2, h//2), 1, dtype='f2')
            v_tex = self._ctx.texture((w//2, h//2), 1, dtype='f2')

            y_tex.write((y_plane.astype(np.float32) / 1023.0).astype(np.float16).tobytes())
            u_tex.write((u_plane.astype(np.float32) / 1023.0).astype(np.float16).tobytes())
            v_tex.write((v_plane.astype(np.float32) / 1023.0).astype(np.float16).tobytes())
            yuv_time_ms = (time.perf_counter() - start_yuv) * 1000.0

            # 测试 RGB 上传
            start_rgb = time.perf_counter()
            rgb_tex = self._ctx.texture((w, h), 3, dtype='f4')
            rgb_tex.write(rgb_data.tobytes())
            rgb_time_ms = (time.perf_counter() - start_rgb) * 1000.0

            # 清理
            y_tex.release()
            u_tex.release()
            v_tex.release()
            rgb_tex.release()

            # YUV 数据大小计算
            yuv_size = y_plane.nbytes + u_plane.nbytes + v_plane.nbytes
            rgb_size = rgb_data.nbytes

            key = f"{w}x{h}"
            results[key] = {
                'resolution': (w, h),
                'yuv_time_ms': yuv_time_ms,
                'rgb_time_ms': rgb_time_ms,
                'yuv_data_mb': yuv_size / 1e6,
                'rgb_data_mb': rgb_size / 1e6,
                'data_reduction': (rgb_size - yuv_size) / rgb_size,
                'time_comparison': (rgb_time_ms - yuv_time_ms) / rgb_time_ms,
            }

        self._results['yuv_vs_rgb'] = results
        return {**results, 'yuv_vs_rgb': results}

    def release(self) -> None:
        """释放所有资源"""
        self._pbo_uploader.release()
        self._yuv_uploader.release()


# ================================================================
# 辅助函数
# ================================================================

def create_yuv_frame_from_planes(
    y: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    pixel_format: str = 'yuv420p10le'
) -> DecodedFrame:
    """
    从 Y/U/V 平面创建 DecodedFrame。

    Args:
        y: Y 平面数据
        u: U 平面数据
        v: V 平面数据
        pixel_format: 像素格式名称

    Returns:
        DecodedFrame 对象
    """
    from src.shared.media.metadata import VideoMetadata, Rational

    h, w = y.shape[:2]

    metadata = VideoMetadata(
        width=w,
        height=h,
        fps=Rational(30, 1),
        total_frames=1,
        codec_name='raw',
        codec_profile=None,
        pixel_format=pixel_format,
        bit_depth=10 if '10le' in pixel_format else 8
    )

    frame = DecodedFrame(
        frame_index=0,
        pts=0,
        time_seconds=0.0,
        planes=(y, u, v),
        pixel_format=pixel_format,
        width=w,
        height=h,
        metadata=metadata
    )

    return frame


def create_packed_frame_from_data(
    data: np.ndarray,
    pixel_format: str = 'rgba',
    frame_index: int = 0
) -> DecodedFrame:
    """
    从打包数据创建 DecodedFrame。

    Args:
        data: 打包数据 (H, W, C) RGB 或 RGBA
        pixel_format: 像素格式名称（如 'rgb24', 'rgba', 'bgr24', 'bgra'）
        frame_index: 帧号

    Returns:
        DecodedFrame 对象
    """
    from src.shared.media.metadata import VideoMetadata, Rational

    h, w = data.shape[:2]

    metadata = VideoMetadata(
        width=w,
        height=h,
        fps=Rational(30, 1),
        total_frames=1,
        codec_name='raw',
        codec_profile=None,
        pixel_format=pixel_format,
        bit_depth=8
    )

    frame = DecodedFrame(
        frame_index=frame_index,
        pts=0,
        time_seconds=0.0,
        data=data,
        pixel_format=pixel_format,
        width=w,
        height=h,
        metadata=metadata
    )

    return frame


def dispatch_upload_command(
    decoded_frame: DecodedFrame,
    renderer_submit_func: Any,
    frame_index: Optional[int] = None,
    yuv_matrix: int = 0,
    color_range: int = 0
) -> str:
    """
    P4.A: 根据 DecodedFrame.pixel_format 自动选择正确的上传命令。

    Args:
        decoded_frame: DecodedFrame 对象
        renderer_submit_func: AsyncRenderer.submit 方法
        frame_index: 帧号（可选，从 DecodedFrame 获取）
        yuv_matrix: YUV 转换矩阵（YUV 格式使用）
        color_range: 颜色范围（YUV 格式使用）

    Returns:
        使用的命令名称字符串
    """
    from src.shared.media.frame import get_pixel_format

    if frame_index is None:
        frame_index = decoded_frame.frame_index

    pixel_format = decoded_frame.pixel_format
    format_info = get_pixel_format(pixel_format)

    if format_info is None:
        # 未知格式，使用 fallback
        logger.warning(f"Unknown pixel format {pixel_format}, using UPLOAD_VIDEO_FRAME")
        return 'upload_video_frame'

    if format_info.is_planar:
        # YUV/NV12/P010 平面格式 -> UPLOAD_VIDEO_PLANES
        from src.shared.video.render_commands import RenderCommand
        data = {
            'decoded_frame': decoded_frame,
            'frame_index': frame_index,
            'yuv_matrix': yuv_matrix,
            'color_range': color_range
        }
        renderer_submit_func(RenderCommand.UPLOAD_VIDEO_PLANES, data)
        return 'upload_video_planes'
    else:
        # 打包 RGB/RGBA 格式 -> UPLOAD_DECODED_VIDEO_FRAME
        from src.shared.video.render_commands import RenderCommand
        data = {
            'decoded_frame': decoded_frame,
            'frame_index': frame_index
        }
        renderer_submit_func(RenderCommand.UPLOAD_DECODED_VIDEO_FRAME, data)
        return 'upload_decoded_video_frame'
