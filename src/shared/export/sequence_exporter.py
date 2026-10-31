"""
Image Sequence Exporter Module

Exports individual frames as image sequence.
Supports EXR, TIFF, PNG formats with HDR preservation.

## Supported Formats

- EXR: OpenEXR - Full HDR support, float16/float32, alpha channel
- TIFF: 8-bit, 16-bit, 32-bit float - HDR capable
- PNG: 8-bit standard, 16-bit extended - limited HDR

## HDR Preservation

EXR format preserves full HDR data:
- Scene-linear color space
- Float16 (half) or Float32 precision
- No gamma encoding
- Alpha channel support

TIFF format supports HDR via:
- 16-bit integer (0-65535)
- 32-bit float (full range)
- Optional gamma encoding

PNG format limitations:
- 8-bit: 0-255 per channel (gamma encoded)
- 16-bit: 0-65535 per channel (gamma encoded)

## Naming Patterns

Default: `frame_{:04d}.exr` -> `frame_0000.exr`, `frame_0001.exr`, ...
Custom: Any pattern with `{}` placeholder for frame number.

## Color Policy

Each format has a recommended color processing policy:
- EXR: scene-linear (no gamma)
- TIFF: depends on bit depth
- PNG: sRGB (gamma 2.4)

## Usage Example

```python
from src.shared.export import ImageSequenceExporter
from src.shared.media import DecodedFrame

exporter = ImageSequenceExporter(
    output_dir='/path/to/output',
    format='exr',
    naming='shot01_frame_{:05d}',
    bit_depth=16
)

# Export frames
for i, frame in enumerate(frames):
    path = exporter.export_frame(frame, i)
    print(f'Exported: {path}')
```
"""

from __future__ import annotations

import importlib.util
import os
import logging
import struct
import zlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Any, TYPE_CHECKING, Union

import numpy as np

# Use TYPE_CHECKING for DecodedFrame import (avoids runtime dependency on cv2)
if TYPE_CHECKING:
    from src.shared.media.frame import DecodedFrame

logger = logging.getLogger(__name__)


def _optional_module_available(name: str) -> bool:
    """可选后端依赖的能力探测（AUD-33323）。

    用 ``importlib.util.find_spec`` 只探测不导入：

    - 探测结果与 import 行为一致（发行版/venv 中已装即返回 True）；
    - 杜绝旧实现的两个问题——在 ``except ImportError`` 处理器内再次
      ``import imageio``（同样缺失时新抛的 ImportError 直接逃逸，
      回退链自身成为故障源），以及把"缺依赖"伪装成"格式降级"。

    Args:
        name: 顶层模块名（如 ``'OpenEXR'``、``'tifffile'``、``'imageio'``）

    Returns:
        模块是否已安装
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# Type alias for frame data input
FrameInput = Union[np.ndarray, Any]  # DecodedFrame or raw numpy array


class ImageFormat(Enum):
    """Image format identifiers for sequence export."""
    EXR = 'exr'
    TIFF = 'tiff'
    PNG = 'png'
    JPG = 'jpg'
    DPX = 'dpx'


class ColorPolicy(Enum):
    """Color processing policy for image export."""
    SCENE_LINEAR = 'scene_linear'    # No gamma (EXR)
    SRGB = 'srgb'                     # Gamma 2.4 (PNG/JPG)
    REC709 = 'rec709'                 # Rec.709 transfer (TIFF/PNG)
    LOG = 'log'                       # Log encoding (Cineon/DPX)
    RAW = 'raw'                       # No processing


class BitDepthMode(Enum):
    """Bit depth mode for image export."""
    BIT8 = 8       # Standard 8-bit (PNG, JPG)
    BIT16 = 16     # High precision (TIFF, PNG, EXR half)
    BIT32 = 32     # Full float (TIFF, EXR)


@dataclass
class SequenceExportConfig:
    """
    Configuration for image sequence export.

    Attributes:
        output_dir: Output directory path
        format: Image format (exr, tiff, png)
        naming: Filename pattern with format placeholder
        bit_depth: Bit depth (8, 16, 32)
        color_policy: Color processing policy
        compression: Compression level/format
        metadata: Additional metadata to embed
        overwrite: Whether to overwrite existing files
        start_frame: Starting frame number for naming
    """

    output_dir: str
    format: ImageFormat = ImageFormat.EXR
    naming: str = 'frame_{:04d}'
    bit_depth: BitDepthMode = BitDepthMode.BIT16
    color_policy: ColorPolicy = ColorPolicy.SCENE_LINEAR
    compression: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    overwrite: bool = True
    start_frame: int = 0


class VerifiedFrameManifest:
    """F6.4 · 已验证帧 manifest——恢复依据内容指纹而非文件名存在。

    每帧成功导出后记录 (路径, 大小, 头部哈希)；恢复时文件存在**且**
    指纹匹配才跳过——暂停/取消/断电后损坏帧、截断帧、不同配置的
    残留文件都会被重导（"不跳过坏文件、不混不同配置"）。
    """

    def __init__(self) -> None:
        self._records: "dict[str, tuple[int, str]]" = {}

    @staticmethod
    def _file_signature(path: str) -> "tuple[int, str]":
        import hashlib

        h = hashlib.sha256()
        with open(path, "rb") as fh:
            h.update(fh.read(1 << 20))
        return os.path.getsize(path), h.hexdigest()

    def record(self, path: str) -> None:
        """记录一帧为已验证（导出成功后调用）。"""
        try:
            self._records[path] = self._file_signature(path)
        except OSError:
            self._records.pop(path, None)

    def is_verified(self, path: str) -> bool:
        """文件存在且大小+哈希与记录一致（文件名存在≠已验证）。"""
        record = self._records.get(path)
        if record is None or not os.path.isfile(path):
            return False
        try:
            return self._file_signature(path) == record
        except OSError:
            return False

    def save(self, path: str) -> bool:
        import json

        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"schema": "topos.frame_manifest/1",
                     "frames": {p: list(sig) for p, sig in
                                self._records.items()}},
                    fh)
            return True
        except OSError:
            return False

    @classmethod
    def load(cls, path: str) -> "VerifiedFrameManifest":
        import json

        manifest = cls()
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            if payload.get("schema") == "topos.frame_manifest/1":
                manifest._records = {
                    p: (int(sig[0]), str(sig[1]))
                    for p, sig in (payload.get("frames") or {}).items()}
        except (OSError, ValueError, TypeError):
            pass  # 缺失/损坏 manifest = 全部重导（fail-safe）
        return manifest

    def __len__(self) -> int:
        return len(self._records)


class ImageSequenceExporter:
    """
    Image sequence exporter with HDR support.

    Supports EXR/TIFF/PNG formats with proper color handling.
    Preserves HDR data when using EXR or 16-bit TIFF.

    Attributes:
        config: Export configuration
        _output_dir: Output directory path
        _format: Image format string
        _naming: Filename naming pattern
        _bit_depth: Bit depth for output
        _color_policy: Color processing policy
    """

    def __init__(
        self,
        output_dir: str,
        format: str = 'exr',
        naming: str = 'frame_{:04d}',
        bit_depth: int = 16,
        color_policy: Optional[str] = None,
        compression: Optional[str] = None,
        overwrite: bool = True
    ) -> None:
        """
        Initialize image sequence exporter.

        Args:
            output_dir: Output directory path
            format: Image format ('exr', 'tiff', 'png')
            naming: Filename pattern (e.g., 'frame_{:04d}')
            bit_depth: Bit depth (8, 16, 32)
            color_policy: Color policy override ('scene_linear', 'srgb')
            compression: Compression setting (format-specific)
            overwrite: Whether to overwrite existing files
        """
        self._output_dir = output_dir
        self._format = format.lower()
        self._naming = naming
        self._bit_depth = bit_depth
        self._compression = compression
        self._overwrite = overwrite

        # Determine color policy
        if color_policy:
            self._color_policy = ColorPolicy(color_policy)
        else:
            self._color_policy = self._get_default_color_policy()

        # Ensure output directory exists
        self._ensure_output_dir()

        # Track exported files
        self._exported_files: List[str] = []
        # F6.4：已验证帧 manifest（None = 旧行为按文件名跳过）。
        # M10：构造时从输出目录装载既有 manifest——暂停/重启后续导按
        # 指纹跳过已验证帧（此前只在单次会话内记录，重启后 manifest
        # 为空、已验证帧被重导）。缺失/损坏 = 全部重导（fail-safe）。
        self._frame_manifest = VerifiedFrameManifest.load(
            os.path.join(self._output_dir, "frame_manifest.json"))

    def _ensure_output_dir(self) -> None:
        """Create output directory if it doesn't exist."""
        if not os.path.exists(self._output_dir):
            os.makedirs(self._output_dir, exist_ok=True)
            logger.info(f"Created output directory: {self._output_dir}")

    def _get_default_color_policy(self) -> ColorPolicy:
        """
        Get default color policy for format.

        Returns:
            Default ColorPolicy for current format
        """
        policy_map = {
            'exr': ColorPolicy.SCENE_LINEAR,
            'tiff': ColorPolicy.SCENE_LINEAR if self._bit_depth >= 16 else ColorPolicy.SRGB,
            'png': ColorPolicy.SRGB,
            'jpg': ColorPolicy.SRGB,
            'dpx': ColorPolicy.LOG,
        }
        return policy_map.get(self._format, ColorPolicy.SRGB)

    def get_color_policy(self) -> str:
        """
        Return color processing policy name.

        Returns:
            Color policy string identifier
        """
        return self._color_policy.value

    def get_extension(self) -> str:
        """
        Get file extension for format.

        Returns:
            Extension string (e.g., '.exr')
        """
        ext_map = {
            'exr': '.exr',
            'tiff': '.tiff',
            'tif': '.tif',
            'png': '.png',
            'jpg': '.jpg',
            'jpeg': '.jpg',
            'dpx': '.dpx',
        }
        return ext_map.get(self._format, f'.{self._format}')

    def generate_filename(self, frame_index: int) -> str:
        """
        Generate filename for frame.

        Args:
            frame_index: Frame number (0-indexed)

        Returns:
            Filename string
        """
        try:
            name = self._naming.format(frame_index)
        except (ValueError, KeyError):
            # Fallback to simple naming if pattern fails
            name = f'frame_{frame_index:04d}'
        return f'{name}{self.get_extension()}'

    def export_frame(
        self,
        frame: 'DecodedFrame',
        frame_index: int,
        processed_data: Optional[np.ndarray] = None
    ) -> str:
        """
        Export single frame to file.

        Args:
            frame: DecodedFrame instance (from media module)
            frame_index: Frame number for naming
            processed_data: Pre-processed RGBA array (optional)

        Returns:
            Output file path

        Raises:
            IOError: If export fails
        """
        # Generate filename and path
        filename = self.generate_filename(frame_index)
        filepath = os.path.join(self._output_dir, filename)

        # Check for existing file
        # F6.4：跳过条件 = 文件存在 且 manifest 指纹验证通过——损坏/
        # 截断/异配置残留文件重导（不跳过坏文件、不混不同配置）。
        if os.path.exists(filepath) and not self._overwrite:
            if self._frame_manifest.is_verified(filepath):
                logger.warning(f"Skipping existing file: {filepath}")
                return filepath
            logger.info(
                "F6.4 resume: %s exists but fails manifest verification; "
                "re-exporting", filepath)

        # Get frame data
        if processed_data is not None:
            data = processed_data
        else:
            data = self._extract_frame_data(frame)

        # Apply color policy
        data = self._apply_color_policy(data)

        # Export based on format
        try:
            if self._format == 'exr':
                self._export_exr(filepath, data, frame)
            elif self._format in ('tiff', 'tif'):
                self._export_tiff(filepath, data, frame)
            elif self._format == 'png':
                self._export_png(filepath, data, frame)
            elif self._format in ('jpg', 'jpeg'):
                self._export_jpg(filepath, data, frame)
            elif self._format == 'dpx':
                self._export_dpx(filepath, data, frame)
            else:
                # Fallback to PIL-based export
                self._export_pil(filepath, data)

            self._exported_files.append(filepath)
            # F6.4：成功导出即记录为已验证帧（恢复按指纹不按文件名）。
            # M10：逐帧落盘 manifest——导出中途崩溃/断电后重启，已验证
            # 帧经 load() 装载并跳过（此前只在内存记录，会话结束即丢）。
            if self._frame_manifest is not None:
                self._frame_manifest.record(filepath)
                self._frame_manifest.save(
                    os.path.join(self._output_dir, "frame_manifest.json"))
            logger.debug(f"Exported frame {frame_index}: {filepath}")

        except Exception as e:
            logger.error(f"Failed to export frame {frame_index}: {e}")
            raise IOError(f"Frame export failed: {e}") from e

        return filepath

    def _extract_frame_data(self, frame: 'DecodedFrame') -> np.ndarray:
        """
        Extract RGBA data from DecodedFrame.

        Args:
            frame: DecodedFrame instance (from media module)

        Returns:
            RGBA numpy array (H, W, 4)
        """
        if frame.is_packed and frame.data is not None:
            data = frame.data
            # AUD-33320：packed 整数帧数据必须按 dtype 归一化
            # （uint8/255、uint16/65535，float 原样）——下游
            # _apply_color_policy 与 _export_png 的 clip(0,1) 均假定
            # [0,1] float 域，整数原样透传会把 uint8 导出成全白图像
            # （clip(0,255)→1，255 倍过曝）。
            if data.dtype == np.uint8:
                data = data.astype(np.float32) * (1.0 / 255.0)
            elif data.dtype == np.uint16:
                data = data.astype(np.float32) * (1.0 / 65535.0)
            elif np.issubdtype(data.dtype, np.integer):
                max_val = float(np.iinfo(data.dtype).max)
                data = data.astype(np.float32) * (1.0 / max_val)
            # Ensure RGBA format（归一化后补 alpha，保证 alpha=1.0）
            if data.shape[-1] == 3:
                # Add alpha channel
                alpha = np.ones(data.shape[:2], dtype=np.float32)
                data = np.dstack([data, alpha])
            return data

        elif frame.is_planar and frame.planes:
            # Convert planar YUV/YUVA to RGBA
            return self._planar_to_rgba(frame)

        # Fallback: create empty array
        logger.warning(f"Frame {frame.frame_index} has no valid data")
        return np.zeros((frame.height, frame.width, 4), dtype=np.float32)

    def _planar_to_rgba(self, frame: 'DecodedFrame') -> np.ndarray:
        """
        Convert planar YUV/YUVA to RGBA.

        Args:
            frame: DecodedFrame with planar data (from media module)

        Returns:
            RGBA numpy array
        """
        planes = frame.planes
        h, w = frame.height, frame.width

        # Y plane
        y = planes[0].astype(np.float32)

        # Normalize based on bit depth（先算——灰度分支的中性色度也依赖它）
        max_val = (2 ** frame.bit_depth) - 1 if frame.bit_depth else 255

        # U, V planes (may be subsampled)
        if len(planes) >= 3:
            u = planes[1].astype(np.float32)
            v = planes[2].astype(np.float32)

            # Handle subsampling (upsample chroma)
            if u.shape != (h, w):
                scale_h = h // u.shape[0]
                scale_w = w // u.shape[1]
                u = np.repeat(np.repeat(u, scale_h, axis=0), scale_w, axis=1)
                v = np.repeat(np.repeat(v, scale_h, axis=0), scale_w, axis=1)
        else:
            # AUD-33321：少于 3 平面（frame.py 契约平面格式为 2-4 平面，
            # 2 = Y+A 之类）没有色度平面——按灰度处理（中性色度 →
            # R=G=B=Y）。此前 len(planes)<3 时 u/v 未绑定即被下文引用，
            # 灰度/双平面帧导出必然 UnboundLocalError（被 export_frame
            # 包装成 IOError"Frame export failed"，不指明根因）。
            neutral = np.full((h, w), max_val * 0.5, dtype=np.float32)
            u = neutral
            v = neutral

        # Alpha plane (if present)
        alpha = None
        if len(planes) >= 4:
            alpha = planes[3].astype(np.float32)
            if alpha.shape != (h, w):
                scale_h = h // alpha.shape[0]
                scale_w = w // alpha.shape[1]
                alpha = np.repeat(np.repeat(alpha, scale_h, axis=0), scale_w, axis=1)

        # Convert YUV to RGB (BT.709 coefficients)
        # R = Y + 1.5748 * (V - 0.5)
        # G = Y - 0.1873 * (U - 0.5) - 0.4681 * (V - 0.5)
        # B = Y + 1.8556 * (U - 0.5)

        y_norm = y / max_val
        u_norm = u / max_val - 0.5
        v_norm = v / max_val - 0.5

        # BT.709 YUV to RGB
        r = y_norm + 1.5748 * v_norm
        g = y_norm - 0.1873 * u_norm - 0.4681 * v_norm
        b = y_norm + 1.8556 * u_norm

        # Stack RGB
        rgb = np.stack([r, g, b], axis=-1)

        # Add alpha
        if alpha is not None:
            alpha_norm = alpha / max_val
        else:
            alpha_norm = np.ones((h, w), dtype=np.float32)

        rgba = np.dstack([rgb, alpha_norm])

        # Convert to output dtype
        return rgba.astype(np.float32)

    def _apply_color_policy(self, data: np.ndarray) -> np.ndarray:
        """
        Apply color processing policy to data.

        Args:
            data: RGBA float array

        Returns:
            Processed RGBA array
        """
        if self._color_policy == ColorPolicy.SCENE_LINEAR:
            # No processing - keep linear
            return data

        elif self._color_policy == ColorPolicy.SRGB:
            # Apply sRGB gamma (OETF)
            # sRGB OETF: V = 1.055 * L^(1/2.4) - 0.055 for L > 0.0031308
            #            V = 12.92 * L for L <= 0.0031308
            linear_mask = data[..., :3] <= 0.0031308
            gamma_mask = ~linear_mask

            result = data.copy()
            rgb = result[..., :3]

            # Linear segment
            rgb[linear_mask] = 12.92 * rgb[linear_mask]

            # Gamma segment
            rgb[gamma_mask] = 1.055 * np.power(rgb[gamma_mask], 1.0 / 2.4) - 0.055

            return result

        elif self._color_policy == ColorPolicy.REC709:
            # Rec.709 gamma (similar to sRGB but different constants)
            linear_mask = data[..., :3] <= 0.018
            gamma_mask = ~linear_mask

            result = data.copy()
            rgb = result[..., :3]

            # Linear segment
            rgb[linear_mask] = 4.5 * rgb[linear_mask]

            # Gamma segment
            rgb[gamma_mask] = 1.099 * np.power(rgb[gamma_mask], 0.45) - 0.099

            return result

        elif self._color_policy == ColorPolicy.LOG:
            # Cineon Log encoding (for DPX)
            # Log V = (685 * (10^(0.0026 * L - 0.073)) - 685) / 1023
            # Simplified approximation
            result = data.copy()
            rgb = result[..., :3]

            # Clip to valid range
            rgb = np.clip(rgb, 0.0, 2.0)

            # Log encoding (approximation)
            log_val = np.log10(rgb + 0.01) * 0.5 + 0.5
            rgb = np.clip(log_val, 0.0, 1.0)

            return result

        # Default: no processing
        return data

    def _export_exr(
        self,
        filepath: str,
        data: np.ndarray,
        frame: Optional['DecodedFrame']
    ) -> None:
        """
        Export to OpenEXR format (HDR).

        Uses OpenEXR library if available, otherwise PIL fallback.
        Preserves float16/float32 precision.

        Args:
            filepath: Output file path
            data: RGBA float array
            frame: Frame metadata (optional, from media module)
        """
        # AUD-33323：先探测能力、后选择后端——此前 OpenEXR 缺失时在
        # except ImportError 处理器内 import imageio（同样缺失时新抛
        # ImportError 逃逸），最终还会静默降级改写 .tiff 路径，返回/
        # 登记路径与磁盘不符（AUD-33324）。依赖缺失时显式报
        # "不支持"，不再静默换格式；编码失败原样上抛（export_frame
        # 统一包装为 IOError 并带原因）。
        if _optional_module_available("OpenEXR") and _optional_module_available("Imath"):
            # Try to use OpenEXR library
            import OpenEXR
            import Imath

            # Determine precision
            if self._bit_depth == 32:
                pt = Imath.PixelType(Imath.PixelType.FLOAT)
                dtype = np.float32
            else:
                pt = Imath.PixelType(Imath.PixelType.HALF)
                dtype = np.float16

            h, w = data.shape[:2]

            # Convert to float16/32
            data_out = data.astype(dtype)

            # Separate channels
            r = data_out[..., 0].flatten()
            g = data_out[..., 1].flatten()
            b = data_out[..., 2].flatten()
            a = data_out[..., 3].flatten()

            # Create EXR header
            header = OpenEXR.Header(w, h)
            header['channels'] = {
                'R': Imath.Channel(pt),
                'G': Imath.Channel(pt),
                'B': Imath.Channel(pt),
                'A': Imath.Channel(pt),
            }

            # Write EXR
            exr_file = OpenEXR.OutputFile(filepath, header)
            exr_file.writePixels({
                'R': r.tobytes(),
                'G': g.tobytes(),
                'B': b.tobytes(),
                'A': a.tobytes(),
            })
            exr_file.close()
        elif _optional_module_available("imageio"):
            # imageio supports EXR via OpenEXR backend
            import imageio
            imageio.imwrite(filepath, data.astype(np.float16))
        else:
            raise ValueError(
                "EXR 导出需要 OpenEXR 或 imageio 后端（当前环境均未"
                "安装）；请安装 openexr/imageio 或改用其他导出格式")

    def _export_tiff(
        self,
        filepath: str,
        data: np.ndarray,
        frame: Optional['DecodedFrame'] = None
    ) -> None:
        """
        Export to TIFF format.

        Supports 8-bit, 16-bit, and 32-bit float.

        Args:
            filepath: Output file path
            data: RGBA array
            frame: Frame metadata (optional, from media module)
        """
        from PIL import Image

        # AUD-33323/33324：能力先探测、缺后端显式报错——此前 16-bit/32-bit
        # 路径在 except ImportError 内再 import imageio（缺失时新 ImportError
        # 逃逸），或降级 8-bit PIL；外层宽泛 except Exception 还会把磁盘/
        # 权限/形状错误全部吞成"静默改写 .png"，export_frame 登记的 .tiff
        # 路径在磁盘上并不存在。
        if self._bit_depth == 32:
            # Float TIFF - use tifffile or imageio
            if _optional_module_available("tifffile"):
                import tifffile
                tifffile.imwrite(filepath, data.astype(np.float32))
            elif _optional_module_available("imageio"):
                import imageio
                imageio.imwrite(filepath, data.astype(np.float32))
            else:
                raise ValueError(
                    "32-bit float TIFF 导出需要 tifffile 或 imageio 后端"
                    "（当前环境均未安装）；请安装后端或改用其他导出格式")

        elif self._bit_depth == 16:
            # 16-bit TIFF
            data_16 = np.clip(data, 0.0, 1.0)
            data_16 = (data_16 * 65535).astype(np.uint16)

            # PIL mode 'I;16' for 16-bit grayscale
            # For RGBA, need to save channels separately or use tifffile
            if _optional_module_available("tifffile"):
                import tifffile
                tifffile.imwrite(filepath, data_16)
            elif _optional_module_available("imageio"):
                import imageio
                imageio.imwrite(filepath, data_16)
            else:
                raise ValueError(
                    "16-bit TIFF 导出需要 tifffile 或 imageio 后端"
                    "（当前环境均未安装）；请安装后端或改用其他导出格式")

        else:
            # 8-bit TIFF
            data_8 = np.clip(data, 0.0, 1.0)
            data_8 = (data_8 * 255).astype(np.uint8)
            img = Image.fromarray(data_8, mode='RGBA')
            img.save(filepath, compression=self._compression or 'lzw')

    def _export_png(
        self,
        filepath: str,
        data: np.ndarray,
        frame: Optional['DecodedFrame'] = None
    ) -> None:
        """
        Export to PNG format.

        Supports 8-bit and 16-bit PNG.

        Args:
            filepath: Output file path
            data: RGBA array
            frame: Frame metadata (optional, from media module)
        """
        from PIL import Image

        # Save with optional compression
        compress_level = 6  # Default
        if self._compression:
            if self._compression == 'fast':
                compress_level = 1
            elif self._compression == 'best':
                compress_level = 9
            else:
                compress_level = int(self._compression)

        # Clip to valid range
        data_clipped = np.clip(data, 0.0, 1.0)

        if self._bit_depth >= 16:
            # AUD-33322：Pillow 不支持 16-bit RGBA PNG 编码——
            # Image.fromarray(uint16 RGBA, mode='RGBA;16') 必抛
            # "Cannot handle this data type: (1, 1, 4), <u2"（Pillow
            # 12.x 实测复现），16-bit PNG 导出 100% 失败。改由内置
            # 编码器写出（PNG spec：bit depth 16 / color type 6 RGBA，
            # 大端样本、filter 0、无隔行），无新增依赖。
            data_16 = (data_clipped * 65535).astype(np.uint16)
            self._write_png16_rgba(filepath, data_16, compress_level)
        else:
            # 8-bit PNG
            data_8 = (data_clipped * 255).astype(np.uint8)
            img = Image.fromarray(data_8, mode='RGBA')
            img.save(filepath, compress_level=compress_level)

    @staticmethod
    def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
        """PNG chunk 装配：length + type + payload + CRC32(type+payload)。"""
        return (
            struct.pack(">I", len(payload))
            + chunk_type
            + payload
            + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
        )

    @classmethod
    def _write_png16_rgba(
        cls,
        filepath: str,
        data_16: np.ndarray,
        compress_level: int,
    ) -> None:
        """写 16-bit RGBA PNG（AUD-33322；Pillow 无此编码能力）。

        Args:
            filepath: Output file path
            data_16: (H, W, 4) uint16 RGBA 数组
            compress_level: zlib 压缩级别（1-9，与 8-bit 路径同锚）

        Raises:
            ValueError: 输入数组形状/dtype 不符
        """
        if data_16.dtype != np.uint16 or data_16.ndim != 3 or data_16.shape[2] != 4:
            raise ValueError(
                f"16-bit PNG 编码需要 (H, W, 4) uint16 RGBA 数组，"
                f"got dtype={data_16.dtype} shape={data_16.shape}")

        height, width = data_16.shape[:2]
        # PNG 样本为大端 16-bit；每行前置 filter type 0（None）
        big_endian = np.ascontiguousarray(data_16, dtype=">u2")
        raw = b"".join(
            b"\x00" + big_endian[row].tobytes() for row in range(height)
        )

        ihdr = struct.pack(">IIBBBBB", width, height, 16, 6, 0, 0, 0)
        with open(filepath, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
            handle.write(cls._png_chunk(b"IHDR", ihdr))
            handle.write(cls._png_chunk(b"IDAT", zlib.compress(raw, compress_level)))
            handle.write(cls._png_chunk(b"IEND", b""))

    def _export_jpg(
        self,
        filepath: str,
        data: np.ndarray,
        frame: Optional['DecodedFrame'] = None
    ) -> None:
        """
        Export to JPEG format.

        JPEG is 8-bit only, no alpha support.

        Args:
            filepath: Output file path
            data: RGBA array
            frame: Frame metadata (optional, from media module)
        """
        from PIL import Image

        # JPEG doesn't support alpha - use RGB
        rgb = np.clip(data[..., :3], 0.0, 1.0)
        rgb_8 = (rgb * 255).astype(np.uint8)

        img = Image.fromarray(rgb_8, mode='RGB')

        # Quality setting
        quality = 95
        if self._compression:
            quality = int(self._compression)

        img.save(filepath, quality=quality, subsampling=0)  # 4:4:4

    def _export_dpx(
        self,
        filepath: str,
        data: np.ndarray,
        frame: Optional['DecodedFrame'] = None
    ) -> None:
        """
        Export to DPX format (10-bit log).

        Args:
            filepath: Output file path
            data: RGBA array (log-encoded)
            frame: Frame metadata (optional, from media module)
        """
        # AUD-33323/33324：imageio 是 DPX 的唯一后端——先探测能力，
        # 缺失时显式报"不支持"（此前静默改写 .tiff 路径，export_frame
        # 返回/登记的 .dpx 路径在磁盘上并不存在）。
        if not _optional_module_available("imageio"):
            raise ValueError(
                "DPX 导出需要 imageio 后端（当前环境未安装）；"
                "请安装 imageio 或改用其他导出格式")

        import imageio

        # DPX typically uses 10-bit packing
        # Scale to 10-bit range (0-1023)
        data_10 = np.clip(data[..., :3], 0.0, 1.0)
        data_10 = (data_10 * 1023).astype(np.uint16)

        imageio.imwrite(filepath, data_10)

    def _export_pil(self, filepath: str, data: np.ndarray) -> None:
        """
        Generic PIL-based export fallback.

        Args:
            filepath: Output file path
            data: RGBA array
        """
        from PIL import Image

        # Convert to 8-bit RGBA
        data_8 = np.clip(data, 0.0, 1.0)
        data_8 = (data_8 * 255).astype(np.uint8)

        img = Image.fromarray(data_8, mode='RGBA')
        img.save(filepath)

    def get_exported_files(self) -> List[str]:
        """
        Get list of exported file paths.

        Returns:
            List of file paths
        """
        return self._exported_files.copy()

    def get_export_stats(self) -> dict:
        """
        Get export statistics.

        Returns:
            Dictionary with export statistics
        """
        if not self._exported_files:
            return {
                'count': 0,
                'total_bytes': 0,
                'format': self._format,
                'bit_depth': self._bit_depth,
                'color_policy': self._color_policy.value,
            }

        total_bytes = 0
        for path in self._exported_files:
            if os.path.exists(path):
                total_bytes += os.path.getsize(path)

        return {
            'count': len(self._exported_files),
            'total_bytes': total_bytes,
            'avg_bytes': total_bytes / len(self._exported_files),
            'format': self._format,
            'bit_depth': self._bit_depth,
            'color_policy': self._color_policy.value,
            'output_dir': self._output_dir,
        }

    def cleanup(self) -> None:
        """
        Clean up exported files (optional).

        Removes all exported files from output directory.
        """
        for path in self._exported_files:
            if os.path.exists(path):
                os.remove(path)
                logger.debug(f"Removed: {path}")

        self._exported_files.clear()
        logger.info(f"Cleaned up {len(self._exported_files)} exported files")


def create_sequence_exporter(
    output_dir: str,
    format: str = 'exr',
    use_case: str = 'vfx',
    **kwargs: Any
) -> ImageSequenceExporter:
    """
    Create image sequence exporter with recommended settings.

    Args:
        output_dir: Output directory
        format: Image format
        use_case: 'vfx', 'web', 'archive'
        **kwargs: Additional exporter options

    Returns:
        ImageSequenceExporter instance
    """
    # Recommended settings by use case
    if use_case == 'vfx':
        # VFX: EXR, 16-bit, scene-linear
        return ImageSequenceExporter(
            output_dir=output_dir,
            format='exr',
            bit_depth=16,
            color_policy='scene_linear',
            **kwargs
        )

    elif use_case == 'web':
        # Web: PNG, 8-bit, sRGB
        return ImageSequenceExporter(
            output_dir=output_dir,
            format='png',
            bit_depth=8,
            color_policy='srgb',
            compression='fast',
            **kwargs
        )

    elif use_case == 'archive':
        # Archive: TIFF, 16-bit, scene-linear
        return ImageSequenceExporter(
            output_dir=output_dir,
            format='tiff',
            bit_depth=16,
            color_policy='scene_linear',
            compression='lzw',
            **kwargs
        )

    # Default: use provided format
    return ImageSequenceExporter(
        output_dir=output_dir,
        format=format,
        **kwargs
    )