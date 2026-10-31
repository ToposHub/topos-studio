"""
Video Decoder Adapter Module
适配 MediaSource 到旧 VideoDecoder 接口，实现平滑过渡。

## 职责

- VideoDecoderAdapter: 包装 MediaSource，提供兼容旧代码的接口

## 兼容接口

旧 VideoDecoder 接口（已实现）：
- get_frame(frame_number) -> np.ndarray (BGR uint8)
- get_metadata() -> dict (通过属性)
- total_frames -> int
- fps -> float
- width -> int
- height -> int
- duration -> float

内部转换：
- MediaSource.read_frame() -> DecodedFrame
- DecodedFrame -> np.ndarray (BGR uint8)

## 使用示例

```python
from src.shared.media.adapter import VideoDecoderAdapter

# 创建 Adapter（旧代码可以直接使用）
adapter = VideoDecoderAdapter('/path/to/video.mp4')
frame = adapter.get_frame(0)  # 返回 BGR uint8 数组
print(f"总帧数: {adapter.total_frames}, 帧率: {adapter.fps}")
adapter.release()
```

## 设计原则

- 对外接口与 VideoDecoder 完全一致
- 内部使用 MediaSourceFactory
- 不修改现有播放代码
- 支持后端切换（通过 Factory）
"""

import logging
from typing import Optional, Any, TYPE_CHECKING

import numpy as np

from src.shared.color.types import ColorMatrix, ColorRange

from .color_decode import get_color_converter
from .errors import MediaError
from .factory import MediaSourceFactory
from .source import MediaSource, MediaSourceError
from .frame import DecodedFrame

if TYPE_CHECKING:
    from .metadata import VideoMetadata

logger = logging.getLogger(__name__)

# 元数据色彩字段（标准名字符串）→ ColorMatrix 枚举。未收录名称回退
# BT601（旧 VideoDecoder 兼容路径的"数字 601 limited-range"默认实现）。
_MATRIX_BY_STANDARD_NAME: dict[str, ColorMatrix] = {
    'bt709': ColorMatrix.BT709,
    'bt2020nc': ColorMatrix.BT2020_NC,
    'bt2020c': ColorMatrix.BT2020_C,
    'smpte170m': ColorMatrix.BT601,
    'bt470bg': ColorMatrix.BT601_PAL,
    'fcc': ColorMatrix.FCC,
    'smpte240m': ColorMatrix.SMPTE240M,
    'ycgco': ColorMatrix.YCGCO,
}

_RANGE_BY_STANDARD_NAME: dict[str, ColorRange] = {
    'limited': ColorRange.LIMITED,
    'full': ColorRange.FULL,
}


class VideoDecoderAdapter:
    """
    适配 MediaSource 到旧 VideoDecoder 接口。

    目的：让旧播放代码先能跑，不一次性改完整 UI。

    旧接口（兼容）：
    - get_frame(frame_number) -> np.ndarray
    - get_metadata() -> dict（通过属性）
    - total_frames -> int
    - fps -> float
    - width -> int
    - height -> int
    - duration -> float

    内部：
    - 使用 MediaSourceFactory 创建源
    - 转换 DecodedFrame -> np.ndarray (BGR uint8 for compatibility)
    """

    def __init__(
        self,
        path: str,
        backend: str = 'auto',
        target_size: Optional[tuple[int, int]] = None,
        preserve_depth: bool = False
    ) -> None:
        """
        初始化视频解码适配器。

        Args:
            path: 视频文件路径
            backend: 后端选择 ('auto', 'ffmpeg', 'gstreamer')
            target_size: 可选的目标尺寸 (width, height)，用于代理缩放
            preserve_depth: 是否保留高位深（10/12/16-bit），默认 False（降为 uint8）

        Raises:
            MediaOpenError: 打开失败
        """
        self._path = path
        self._backend = backend
        self._target_size = target_size
        self._preserve_depth = preserve_depth

        # 使用 Factory 创建 MediaSource
        # P3.A: create() 只创建不 open，需要显式打开
        self._source: MediaSource = MediaSourceFactory.create(
            path,
            backend=backend,
            target_size=target_size
        )
        self._source.open()

        # 缓存元数据
        self._metadata_cache: Optional[dict[str, Any]] = None

        # AUD-34301：probe 失败状态缓存（失败后属性不再重复探测）
        self._probe_failed: bool = False

        # 缓存帧数据转换缓冲区（用于高位深转换）
        self._frame_buffer: Optional[np.ndarray] = None
        self._buffer_size: tuple[int, int] = (0, 0)

        # 当前帧号
        self._current_frame: int = 0

        # 最后帧的位深信息（用于高位深保留模式）
        self._last_frame_depth: int = 8

        logger.info(f"VideoDecoderAdapter: 创建成功，后端={backend}, 路径={path}, 保留位深={preserve_depth}")

    def get_frame(
        self,
        frame_number: int,
        preserve_depth: Optional[bool] = None
    ) -> Optional[np.ndarray]:
        """
        获取指定帧。

        Args:
            frame_number: 帧号（从 0 开始）
            preserve_depth: 是否保留高位深，None 时使用初始化时的设置

        Returns:
            图像数组，格式取决于 preserve_depth：
            - preserve_depth=False: BGR uint8 (H, W, 3)
            - preserve_depth=True: 保持原位深，BGR/RGB uint16 或 float32
            失败返回 None
        """
        try:
            # 读取 DecodedFrame
            decoded_frame = self._source.read_frame(frame_number)

            # 确定是否保留位深
            preserve = preserve_depth if preserve_depth is not None else self._preserve_depth

            if preserve:
                # 保留高位深
                frame = self._convert_preserving_depth(decoded_frame)
            else:
                # 转换为 BGR uint8 格式（旧代码期望的格式）
                frame = self._convert_to_bgr8(decoded_frame)

            self._current_frame = frame_number
            return frame

        except ValueError as e:
            # 帧号超出范围
            logger.warning(f"帧号超出范围: {frame_number}, 错误: {e}")
            return None
        except Exception as e:
            logger.error(f"获取帧失败: {frame_number}, 错误: {e}")
            return None

    def _convert_to_bgr8(self, frame: DecodedFrame) -> np.ndarray:
        """
        转换 DecodedFrame 为旧格式 (BGR uint8)。

        Args:
            frame: DecodedFrame 实例

        Returns:
            BGR uint8 数组 (H, W, 3)
        """
        # 检查数据格式
        if frame.is_packed and frame.data is not None:
            data = frame.data

            # 检查是否已经是 BGR/BGRA uint8
            if data.dtype == np.uint8:
                if frame.pixel_format in ('bgr24', 'bgra'):
                    # BGR24: 直接返回
                    if data.shape[2] == 3:
                        return data
                    # BGRA: 去掉 Alpha
                    elif data.shape[2] == 4:
                        return data[:, :, :3]

                elif frame.pixel_format in ('rgb24', 'rgba'):
                    # RGB -> BGR
                    return np.flip(data[:, :, :3], axis=-1)

                elif frame.pixel_format in ('rgb48', 'rgba64', 'rgb48le', 'rgba64le'):
                    # 16-bit RGB -> 8-bit BGR
                    # 先转 8-bit，再翻转通道
                    data_8bit = (data[:, :, :3] >> 8).astype(np.uint8)
                    return np.flip(data_8bit, axis=-1)

            # float32 数据（HDR 或线性）
            elif data.dtype == np.float32 or data.dtype == np.float16:
                # float -> uint8（假设范围 0-1）
                data_clipped = np.clip(data[:, :, :3], 0.0, 1.0)
                data_8bit = (data_clipped * 255.0).astype(np.uint8)
                # 如果是 RGB，转为 BGR
                if frame.pixel_format.startswith('rgb'):
                    return np.flip(data_8bit, axis=-1)
                return data_8bit

        # 平面格式（YUV/GBR 等）——AUD-34300：真实转换（此前依赖
        # DecodedFrame.to_rgb()，而 to_rgb 对 planar 恒返回 None，
        # planar 后端（topos）素材经本适配器取帧恒失败）。
        elif frame.is_planar and frame.planes:
            rgb = self._convert_planar_to_rgb(frame)
            data_8bit = (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
            # RGB -> BGR
            return np.flip(data_8bit, axis=-1)

        # 无法转换
        raise ValueError(f"无法转换帧格式: {frame.pixel_format}, dtype={frame.dtype}")

    def _resolve_color_semantics(
        self,
        frame: DecodedFrame,
    ) -> tuple[ColorMatrix, ColorRange]:
        """
        从帧元数据解析色彩矩阵与范围。

        元数据缺失/未收录时回退旧 VideoDecoder 的"数字 601 limited-range"
        默认实现（不猜其他矩阵，与 .clinerules 色彩元数据一致性约束对齐）。

        Args:
            frame: DecodedFrame 实例

        Returns:
            (ColorMatrix, ColorRange)
        """
        metadata = frame.metadata
        matrix_name = getattr(metadata, 'color_matrix', None) if metadata else None
        range_name = getattr(metadata, 'color_range', None) if metadata else None

        if isinstance(matrix_name, ColorMatrix):
            matrix = matrix_name
        elif isinstance(matrix_name, str):
            matrix = _MATRIX_BY_STANDARD_NAME.get(matrix_name, ColorMatrix.BT601)
        else:
            matrix = ColorMatrix.BT601

        if isinstance(range_name, ColorRange):
            range_ = range_name
        elif isinstance(range_name, str):
            range_ = _RANGE_BY_STANDARD_NAME.get(range_name, ColorRange.LIMITED)
        else:
            range_ = ColorRange.LIMITED

        return matrix, range_

    def _convert_planar_to_rgb(self, frame: DecodedFrame) -> np.ndarray:
        """
        平面格式帧 → RGB float32（AUD-34300）。

        支持两类平面布局：
        - planar RGB（GBR 平面序：G, B, R [, A]）：gbrp*/topos_gbrap* 等；
        - planar YUV（Y, U, V [, A]）：经 color_decode.ColorConverter 做
          矩阵 + range 转换（矩阵/范围取自帧元数据，缺省 601 limited）。

        CFA（TRAW Bayer 相位平面）需要 GPU debayer，本适配器不支持，
        抛领域 ValueError 而非静默返回 None。

        Args:
            frame: planar DecodedFrame（planes 非空）

        Returns:
            RGB 数组 (H, W, 3) float32。整数平面归一化到 [0, 1]
            （YUV 矩阵转换结果可能略越界，由调用方按目标格式处理）。

        Raises:
            ValueError: 平面数不足或该平面格式不受支持
        """
        fmt = frame.pixel_format or ''
        planes = frame.planes

        if fmt.startswith('topos_cfa'):
            raise ValueError(
                f"CFA 平面格式 {fmt} 需要 GPU debayer（cfa_gpu_debayer），"
                f"VideoDecoderAdapter 不支持"
            )

        # 平面序判定：优先像素格式名，其次 plane_infos 分量
        is_gbr_layout = fmt.startswith('gbr') or fmt.startswith('topos_gbr')
        if not is_gbr_layout and frame.plane_infos:
            components = tuple(p.component for p in frame.plane_infos[:3])
            if components == ('G', 'B', 'R'):
                is_gbr_layout = True

        if is_gbr_layout:
            if len(planes) < 3:
                raise ValueError(
                    f"平面数不足: {fmt} 需要 G/B/R 3 个平面，实际 {len(planes)}"
                )
            g_plane, b_plane, r_plane = planes[0], planes[1], planes[2]
            stacked = np.dstack(
                [r_plane.astype(np.float32),
                 g_plane.astype(np.float32),
                 b_plane.astype(np.float32)]
            )
            # 整数平面按该位深满刻度归一化；浮点平面（gbrph16le 线性
            # HDR 值域）保持原值，不缩放。
            if np.issubdtype(planes[0].dtype, np.integer):
                bit_depth = frame.bit_depth if frame.bit_depth > 0 else 16
                stacked /= float((1 << bit_depth) - 1)
            return stacked.astype(np.float32)

        # 其余按 planar YUV 处理（Y, U, V [, A]——Alpha 忽略）
        if len(planes) < 3:
            raise ValueError(
                f"平面数不足: {fmt} 按 YUV 至少需要 Y/U/V 3 个平面，"
                f"实际 {len(planes)}"
            )
        y_plane, u_plane, v_plane = planes[0], planes[1], planes[2]
        matrix, range_ = self._resolve_color_semantics(frame)
        # 平面色度子采样（4:2:0/4:2:2 等）由 ColorConverter 负责匹配到亮度几何
        rgb = get_color_converter().yuv_to_rgb(
            y_plane, u_plane, v_plane,
            matrix=matrix,
            range_=range_,
            bit_depth=frame.bit_depth,
        )
        return rgb

    def _detect_output_dtype(self, frame: DecodedFrame) -> tuple[np.dtype, int]:
        """
        检测帧数据的输出数据类型和位深度。

        Args:
            frame: DecodedFrame 实例

        Returns:
            (output_dtype, bit_depth) 元组
        """
        # 首先检查实际数据类型
        actual_dtype = None
        if frame.data is not None:
            actual_dtype = frame.data.dtype
        elif frame.planes:
            actual_dtype = frame.planes[0].dtype

        # float32/float16 数据 -> 32-bit (HDR)
        if actual_dtype == np.float32 or actual_dtype == np.float16:
            return np.dtype(np.float32), 32

        # uint8 数据 -> 8-bit
        if actual_dtype == np.uint8:
            return np.dtype(np.uint8), 8

        # uint16 数据：需要从实际数据推断位深度
        if actual_dtype == np.uint16:
            # 从数据推断实际位深度
            bit_depth = self._detect_actual_bit_depth(frame)
            return np.dtype(np.uint16), bit_depth

        # 回退：从 frame.bit_depth 属性推断
        bit_depth = frame.bit_depth

        # 根据位深度决定输出类型
        if bit_depth <= 8:
            return np.dtype(np.uint8), 8
        elif bit_depth <= 16:
            # 10/12/16-bit 输出为 uint16
            return np.dtype(np.uint16), bit_depth
        else:
            # float32/64 输出为 float32
            return np.dtype(np.float32), 32

    def _detect_actual_bit_depth(self, frame: DecodedFrame) -> int:
        """
        推断 uint16 帧的实际位深度（AUD-34304）。

        解析顺序（权威来源优先，一次解析，不做逐帧全量扫描）：
        1. pixel_format 注册表（如 yuv422p10le → 10）
        2. plane_infos[0].bit_depth
        3. metadata.bit_depth
        4. 兜底：数据最大值扫描（暗帧可能误判为低位深，仅在
           前三级均不可用时使用）
        - 10-bit: 最大值 <= 1023
        - 12-bit: 最大值 <= 4095
        - 16-bit: 最大值 > 4095

        Args:
            frame: DecodedFrame 实例

        Returns:
            实际位深度 (10, 12, 16)
        """
        # 1) pixel format 注册表（权威）
        fmt = frame.format_info
        if fmt is not None and fmt.bit_depth:
            return int(fmt.bit_depth)

        # 2) plane_infos（权威）
        if frame.plane_infos:
            plane_depth = frame.plane_infos[0].bit_depth
            if plane_depth:
                return int(plane_depth)

        # 3) metadata
        if frame.metadata and hasattr(frame.metadata, 'bit_depth'):
            metadata_depth = frame.metadata.bit_depth
            if metadata_depth:
                return int(metadata_depth)

        # 4) 兜底：数据最大值扫描（仅当前三级均不可用）
        data = None
        if frame.data is not None:
            data = frame.data
        elif frame.planes:
            # 对于平面格式，检查亮度平面
            data = frame.planes[0]

        if data is None:
            return frame.bit_depth

        max_value = data.max()

        if max_value <= 1023:
            return 10
        elif max_value <= 4095:
            return 12
        else:
            return 16

    def _convert_preserving_depth(self, frame: DecodedFrame) -> np.ndarray:
        """
        转换 DecodedFrame 并保留高位深。

        根据输入格式决定输出：
        - uint8 数据: 输出 BGR uint8
        - uint16 数据 (10/12/16-bit): 输出 BGR uint16
        - float32/float16 数据: 输出 RGB float32 (范围 0-1)

        Args:
            frame: DecodedFrame 实例

        Returns:
            保留位深的图像数组
        """
        output_dtype, bit_depth = self._detect_output_dtype(frame)
        self._last_frame_depth = bit_depth

        # 检查数据格式
        if frame.is_packed and frame.data is not None:
            data = frame.data

            # uint8 数据
            if data.dtype == np.uint8:
                if frame.pixel_format in ('bgr24', 'bgra'):
                    if data.shape[2] == 3:
                        return data
                    elif data.shape[2] == 4:
                        return data[:, :, :3]
                elif frame.pixel_format in ('rgb24', 'rgba'):
                    return np.flip(data[:, :, :3], axis=-1)

            # uint16 数据 (10/12/16-bit)
            elif data.dtype == np.uint16:
                if frame.pixel_format in ('rgb48', 'rgba64', 'rgb48le', 'rgba64le'):
                    # RGB48 -> BGR uint16
                    return np.flip(data[:, :, :3], axis=-1).astype(output_dtype)
                elif frame.pixel_format in ('bgr48', 'bgra64'):
                    # 已经是 BGR 格式
                    if data.shape[2] == 4:
                        return data[:, :, :3].astype(output_dtype)
                    return data[:, :, :3].astype(output_dtype)
                elif frame.pixel_format.startswith('gbrp'):
                    # 平面 RGB 格式（打包后）
                    return np.flip(data[:, :, :3], axis=-1).astype(output_dtype)

            # float32/float16 数据 (HDR)
            elif data.dtype == np.float32 or data.dtype == np.float16:
                # 确保范围在 0-1
                data_clipped = np.clip(data[:, :, :3], 0.0, 1.0).astype(np.float32)
                # HDR 数据通常以 RGB 格式存储
                if frame.pixel_format.startswith('rgb'):
                    return data_clipped
                elif frame.pixel_format.startswith('bgr'):
                    # BGR -> RGB
                    return np.flip(data_clipped, axis=-1)
                return data_clipped

        # 平面格式（YUV/GBR/YUVA 等）——AUD-34300：真实转换并保留位深
        # （此前 to_rgb() 对 planar 恒 None，preserve_depth 承诺对 planar
        # 素材完全无法兑现）。
        elif frame.is_planar and frame.planes:
            rgb = self._convert_planar_to_rgb(frame)

            if output_dtype == np.uint16:
                bit_depth = frame.bit_depth if 0 < frame.bit_depth <= 16 else 16
                scale = float((1 << bit_depth) - 1)
                out = np.clip(rgb * scale, 0.0, scale).astype(np.uint16)
                # RGB -> BGR
                return np.flip(out, axis=-1)

            if output_dtype == np.uint8:
                data_8bit = (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
                return np.flip(data_8bit, axis=-1)

            # float 输出（float16/float32 平面，如 gbrph16le 线性 HDR）：
            # 转 float32，不做 [0,1] clamp（线性 HDR 值域可 >1）
            return np.ascontiguousarray(rgb[..., ::-1]).astype(np.float32)

        # 无法处理
        raise ValueError(f"无法转换帧格式（保留位深）: {frame.pixel_format}, dtype={frame.dtype}")

    def get_frame_at_time(self, time_seconds: float) -> Optional[np.ndarray]:
        """
        获取指定时间点的帧。

        Args:
            time_seconds: 时间（秒）

        Returns:
            BGR 图像数组
        """
        metadata = self._source.probe()
        frame_number = metadata.fps.seconds_to_frame(time_seconds)
        return self.get_frame(frame_number)

    def get_thumbnail(
        self,
        frame_number: int = 0,
        max_size: int = 320
    ) -> Optional[np.ndarray]:
        """
        获取缩略图。

        Args:
            frame_number: 帧号
            max_size: 最大尺寸

        Returns:
            缩略图 BGR 数组
        """
        frame = self.get_frame(frame_number)
        if frame is None:
            return None

        h, w = frame.shape[:2]
        if max(h, w) > max_size:
            scale = max_size / max(h, w)
            new_w = int(w * scale)
            new_h = int(h * scale)

            # 使用 numpy 简单缩放（不依赖 cv2）
            # 更精确的缩放需要 cv2.resize 或其他库
            try:
                # 尝试使用 cv2（如果可用）
                import cv2
                return cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
            except ImportError:
                # Fallback: 使用简单的步长采样
                # 这不是理想的缩放方法，但能工作
                step_h = max(1, h // new_h)
                step_w = max(1, w // new_w)
                return frame[::step_h, ::step_w]

        return frame

    def release(self) -> None:
        """释放视频资源"""
        if self._source:
            try:
                self._source.close()
            except Exception as e:
                logger.warning(f"关闭 MediaSource 失败: {e}")
            self._source = None

        self._metadata_cache = None
        self._frame_buffer = None

    def __del__(self) -> None:
        """析构函数"""
        self.release()

    # ================================================================
    # 属性（兼容旧 VideoDecoder 接口）
    # ================================================================

    def _probe_metadata(self) -> Optional['VideoMetadata']:
        """
        带失败缓存的 probe（AUD-34301）。

        - 仅捕获领域异常（MediaError/MediaSourceError）并记录 warning；
          编程错误（RuntimeError/AttributeError 等）正常传播；
        - 失败后置位 ``_probe_failed``，后续属性查询不再重复探测；
        - 已 release（``_source is None``）时返回 None。

        Returns:
            VideoMetadata，失败/已释放时 None
        """
        if self._probe_failed or self._source is None:
            return None
        try:
            return self._source.probe()
        except (MediaError, MediaSourceError) as e:
            logger.warning(f"探测失败: {self._path}, 错误: {e}")
            self._probe_failed = True
            return None

    @property
    def total_frames(self) -> int:
        """获取视频总帧数（探测失败返回 0，不伪造数据）"""
        metadata = self._probe_metadata()
        if metadata is None:
            return 0
        return metadata.total_frames or 0

    @property
    def fps(self) -> float:
        """获取视频帧率（探测失败返回 0.0，不伪造 30.0——AUD-34301）"""
        metadata = self._probe_metadata()
        if metadata is None or metadata.fps is None:
            return 0.0
        return metadata.fps.to_fps()

    @property
    def width(self) -> int:
        """获取视频宽度（探测失败返回 0）"""
        metadata = self._probe_metadata()
        if metadata is None:
            return 0
        return metadata.width

    @property
    def height(self) -> int:
        """获取视频高度（探测失败返回 0）"""
        metadata = self._probe_metadata()
        if metadata is None:
            return 0
        return metadata.height

    @property
    def duration(self) -> float:
        """获取视频时长（秒）"""
        metadata = self._source.probe()
        if metadata.duration_seconds is not None:
            return metadata.duration_seconds
        # 使用帧数和帧率计算
        if self.total_frames > 0 and self.fps > 0:
            return self.total_frames / self.fps
        return 0.0

    @property
    def current_frame(self) -> int:
        """当前帧号"""
        return self._current_frame

    @property
    def path(self) -> str:
        """视频路径"""
        return self._path

    @property
    def backend(self) -> str:
        """当前使用的后端"""
        return self._backend

    @property
    def preserve_depth(self) -> bool:
        """是否保留高位深"""
        return self._preserve_depth

    @property
    def last_frame_depth(self) -> int:
        """
        最后获取帧的位深度。

        Returns:
            位深度值 (8, 10, 12, 16, 32 等)
        """
        return self._last_frame_depth

    # ================================================================
    # 静态工具方法（兼容旧 VideoDecoder）
    # ================================================================

    @staticmethod
    def probe_video_info(path: str) -> Optional[dict]:
        """
        轻量级探测视频属性（不保持解码器打开）。

        Args:
            path: 视频文件路径

        Returns:
            包含视频信息的字典，失败返回 None
        """
        if not VideoDecoderAdapter.is_video_file(path):
            return None

        try:
            adapter = VideoDecoderAdapter(path)
            info = {
                'width': adapter.width,
                'height': adapter.height,
                'fps': adapter.fps,
                'total_frames': adapter.total_frames,
                'duration': adapter.duration
            }
            adapter.release()
            return info
        except Exception as e:
            logger.warning(f"探测视频信息失败: {e}")
            return None

    @staticmethod
    def is_video_file(path: str) -> bool:
        """判断文件是否为支持的视频格式"""
        return MediaSourceFactory.is_video_file(path)

    @staticmethod
    def get_video_info(path: str) -> Optional[dict]:
        """
        获取视频信息而不保持解码器打开。

        Args:
            path: 视频文件路径

        Returns:
            包含视频信息的字典，失败返回 None
        """
        return VideoDecoderAdapter.probe_video_info(path)

    # ================================================================
    # 元数据方法（扩展接口）
    # ================================================================

    def get_metadata(self) -> dict[str, Any]:
        """
        获取完整元数据（扩展接口）。

        Returns:
            包含所有元数据的字典
        """
        if self._metadata_cache:
            return self._metadata_cache

        try:
            metadata = self._source.probe()

            self._metadata_cache = {
                'width': metadata.width,
                'height': metadata.height,
                'fps': metadata.fps.to_fps(),
                'fps_rational': str(metadata.fps),
                'total_frames': metadata.total_frames,
                'duration_seconds': metadata.duration_seconds,
                'codec_name': metadata.codec_name,
                'codec_profile': metadata.codec_profile,
                'pixel_format': metadata.pixel_format,
                'bit_depth': metadata.bit_depth,
                'color_primaries': metadata.color_primaries,
                'color_transfer': metadata.color_transfer,
                'color_matrix': metadata.color_matrix,
                'color_range': metadata.color_range,
                'is_hdr': metadata.is_hdr,
                'is_interlaced': metadata.is_interlaced,
                'rotation': metadata.rotation,
                'backend': self._backend,
            }

            return self._metadata_cache

        except Exception as e:
            logger.warning(f"获取元数据失败: {e}")
            return {
                'width': 0,
                'height': 0,
                'fps': 0.0,
                'total_frames': 0,
                'duration_seconds': 0.0,
                'backend': self._backend,
            }

    def __repr__(self) -> str:
        """字符串表示"""
        return (
            f"VideoDecoderAdapter(path={self._path}, "
            f"backend={self._backend}, "
            f"size={self.width}x{self.height}, "
            f"frames={self.total_frames}, "
            f"preserve_depth={self._preserve_depth})"
        )


# ================================================================
# 兼容接口 wrapper（用于逐步迁移）
# ================================================================

class VideoDecoderWrapper(VideoDecoderAdapter):
    """
    VideoDecoder 完整包装器（用于逐步迁移旧代码）。

    提供与 VideoDecoder 类完全一致的接口，包括信号。

    使用方法：
    ```python
    # 旧代码
    decoder = VideoDecoder(path)

    # 新代码（完全兼容）
    decoder = VideoDecoderWrapper(path)
    ```
    """

    def __init__(
        self,
        path: str,
        target_size: Optional[tuple[int, int]] = None,
        backend: str = 'auto',
        preserve_depth: bool = False
    ) -> None:
        """
        初始化 VideoDecoder wrapper。

        Args:
            path: 视频文件路径
            target_size: 可选的目标尺寸（代理缩放）
            backend: 后端选择
            preserve_depth: 是否保留高位深（10/12/16-bit），默认 False
        """
        super().__init__(path, backend=backend, target_size=target_size, preserve_depth=preserve_depth)

        # 信号支持（通过内部 QObject）
        # 注意：VideoDecoderAdapter 不继承 QObject，
        # 如果需要信号，使用 VideoDecoderWrapper
        # 这里简化处理，不实现信号（可以后续添加）
        self._frame_loaded_callback: Optional[callable] = None

    def set_frame_loaded_callback(self, callback: callable) -> None:
        """
        设置帧加载完成回调（替代信号）。

        Args:
            callback: 回调函数，参数为帧号
        """
        self._frame_loaded_callback = callback

    def get_frame(self, frame_number: int) -> Optional[np.ndarray]:
        """获取指定帧（带回调）"""
        frame = super().get_frame(frame_number)

        if frame is not None and self._frame_loaded_callback:
            try:
                self._frame_loaded_callback(frame_number)
            except Exception:
                pass

        return frame

    # 视频格式扩展列表（与 VideoDecoder.VIDEO_EXTENSIONS 一致）
    VIDEO_EXTENSIONS = [
        '.mp4', '.mov', '.avi', '.mkv', '.flv', '.wmv', '.webm', '.m4v',
        '.mxf', '.ts', '.mts', '.m2ts', '.vob', '.ogv', '.3gp',
        '.prores', '.dnxhd', '.dnxhr',
        '.r3d', '.braw', '.ari', '.cine', '.dng',
    ]

    @staticmethod
    def is_video_file(path: str) -> bool:
        """判断文件是否为支持的视频格式"""
        return any(path.lower().endswith(ext) for ext in VideoDecoderWrapper.VIDEO_EXTENSIONS)