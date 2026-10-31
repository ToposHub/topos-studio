"""
Color Decode Module
YUV range/matrix 转换的 CPU 实现。

## 职责

- ColorConverter: YUV → RGB 转换（CPU fallback）
- Limited/Full range 转换
- BT.709/BT.2020/BT.601 YUV → RGB 矩阵
- PQ/HLG tonemap 预研（CPU 版本）

## 设计原则

- 与 video_yuv.glsl shader 逻辑保持一致
- 支持高位深（10-bit/12-bit/16-bit）
- NumPy 高效实现
- 完整类型注解

## 使用示例

```python
from src.shared.media.color_decode import ColorConverter
from src.shared.color.types import ColorMatrix, ColorRange

converter = ColorConverter()

# YUV → RGB 转换
rgb = converter.yuv_to_rgb(y, u, v, ColorMatrix.BT709, ColorRange.LIMITED)

# Limited → Full range
full_data = converter.limited_to_full(data, is_luma=True)

# PQ → Linear
linear = converter.pq_to_linear(hdr_data)
```
"""

from __future__ import annotations

import logging

import numpy as np
from dataclasses import dataclass
from typing import Tuple, Dict, Optional, Mapping

logger = logging.getLogger(__name__)

from src.shared.color.types import (
    AlphaAssociation,
    ColorMetadataAuthority,
    ColorMatrix,
    ColorPrimaries,
    ColorRange,
    ColorTransfer,
)


class ToposColorMetadataError(ValueError):
    """A file cannot be interpreted without guessing its color semantics."""


@dataclass(frozen=True)
class ToposColorSemantics:
    """Resolved Topos color semantics shared by CPU, preview and host adapters.

    The numeric fields describe storage reconstruction and therefore come from
    TPIC/IDSC.  ``display_authority`` is selected only after RGB exists; an
    ICC profile can describe that RGB, while OCIO remains an explicit label.
    """

    pixel_format: int
    bit_depth: int
    color_range: ColorRange
    color_primaries: ColorPrimaries
    color_transfer: ColorTransfer
    color_matrix: ColorMatrix
    chroma_siting: int
    display_authority: ColorMetadataAuthority
    ocio_colorspace: Optional[str]


_TOPOS_PRIMARIES = {
    1: ColorPrimaries.BT709, 4: ColorPrimaries.BT470M,
    5: ColorPrimaries.BT470BG, 6: ColorPrimaries.SMPTE170M,
    7: ColorPrimaries.SMPTE240M, 8: ColorPrimaries.FILM,
    9: ColorPrimaries.BT2020, 10: ColorPrimaries.XYZ,
    11: ColorPrimaries.P3_DCI, 12: ColorPrimaries.P3_DISPLAY,
    22: ColorPrimaries.EBU3213,
}
_TOPOS_TRANSFERS = {
    1: ColorTransfer.BT709, 4: ColorTransfer.GAMMA22,
    5: ColorTransfer.GAMMA28, 6: ColorTransfer.SMPTE170M,
    7: ColorTransfer.SMPTE240M, 8: ColorTransfer.LINEAR,
    13: ColorTransfer.SRGB, 14: ColorTransfer.BT2020_10,
    15: ColorTransfer.BT2020_12, 16: ColorTransfer.PQ,
    18: ColorTransfer.HLG,
    20: ColorTransfer.TRAW_LOG0,  # TRAW PWL-log12（v1.6 批 1）
}
_TOPOS_MATRICES = {
    0: ColorMatrix.GBR, 1: ColorMatrix.BT709, 4: ColorMatrix.FCC,
    5: ColorMatrix.BT601_PAL, 6: ColorMatrix.BT601,
    7: ColorMatrix.SMPTE240M, 8: ColorMatrix.YCGCO,
    9: ColorMatrix.BT2020_NC, 10: ColorMatrix.BT2020_C,
    14: ColorMatrix.ICTCP,
}


def _validate_ocio_name(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ToposColorMetadataError("OCIO colorspace must be a non-empty UTF-8 string")
    if value.endswith("\x00"):
        value = value[:-1]
    if not value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ToposColorMetadataError("OCIO colorspace contains a control character")
    lowered = value.lower()
    if any(token in value for token in ("/", "\\", ":")) or lowered.endswith(
            (".so", ".dylib", ".dll", ".py", ".sh")):
        raise ToposColorMetadataError("OCIO colorspace must not contain a path or executable suffix")
    return value


def _validate_icc_profile(profile: bytes, pixel_format: int) -> None:
    if len(profile) < 132 or profile[36:40] != b"acsp":
        raise ToposColorMetadataError("ICCP profile is too short or lacks the acsp signature")
    # Both GBR and YUV become RGB before ICC is applied.  An ICC profile for a
    # non-RGB PCS would change the storage contract and is rejected here.
    # H5（D5 软转换）：'CMYK ' 放行 = 打印交付意图元数据（宿主承担
    # CMYK↔RGB 软转换；存储保持 RGB，重建语义不变）。
    if profile[16:20] not in (b"RGB ", b"CMYK"):
        raise ToposColorMetadataError(
            f"ICCP color space {profile[16:20]!r} is incompatible with reconstructed RGB")
    if pixel_format not in (0, 1, 2):
        raise ToposColorMetadataError(f"unsupported TPIC pixel format {pixel_format}")


def resolve_topos_color_semantics(
    idsc: Mapping[str, int],
    *,
    icc_profile: Optional[bytes] = None,
    ocio_colorspace: Optional[str] = None,
    ocio_cicp: Optional[Mapping[str, int]] = None,
) -> ToposColorSemantics:
    """Resolve the frozen TPIM/TPIC color precedence rules.

    ``idsc`` is deliberately mapping-based so ctypes, JSON diagnostics and
    video metadata can all use the same reference implementation.  ICC/OCIO
    never alter range/matrix/pixel-format interpretation.  If an OCIO
    integration supplies a parsed CICP assertion, it must agree with IDSC;
    otherwise a hard conflict is raised instead of falling back to sRGB.
    """
    def integer(name: str) -> int:
        value = idsc.get(name)
        if not isinstance(value, int):
            raise ToposColorMetadataError(f"missing or invalid IDSC field: {name}")
        return value

    pixel_format = integer("pixel_format")
    bit_depth = integer("bit_depth")
    range_id = integer("color_range")
    primaries_id = integer("color_primaries")
    transfer_id = integer("color_transfer")
    matrix_id = integer("color_matrix")
    chroma_siting = integer("chroma_siting") if "chroma_siting" in idsc else 0
    if pixel_format not in (0, 1, 2, 3):
        raise ToposColorMetadataError(f"unsupported TPIC pixel_format={pixel_format}")
    if bit_depth not in (8, 10, 12, 16):
        raise ToposColorMetadataError(f"unsupported TPIC bit depth={bit_depth}")
    if range_id not in (0, 1):
        raise ToposColorMetadataError(f"unknown TPIC color range={range_id}")
    if primaries_id not in _TOPOS_PRIMARIES:
        raise ToposColorMetadataError(f"unknown CICP color primaries={primaries_id}")
    if transfer_id not in _TOPOS_TRANSFERS:
        raise ToposColorMetadataError(f"unknown CICP transfer={transfer_id}")
    if matrix_id not in _TOPOS_MATRICES:
        raise ToposColorMetadataError(f"unknown CICP matrix={matrix_id}")
    if pixel_format in (2, 3) and matrix_id != 0:
        # GBR 与 TRAW CFA（v1.6）同为 identity 契约：无 YUV 矩阵语义
        raise ToposColorMetadataError("GBR/CFA payload must use identity matrix 0")
    if pixel_format not in (2, 3) and matrix_id == 0:
        raise ToposColorMetadataError("YUV payload cannot use GBR identity matrix")
    if icc_profile is not None:
        _validate_icc_profile(icc_profile, pixel_format)
    ocio_value = _validate_ocio_name(ocio_colorspace) if ocio_colorspace is not None else None
    if ocio_cicp is not None:
        for key, actual in (("color_primaries", primaries_id), ("color_transfer", transfer_id),
                            ("color_matrix", matrix_id), ("color_range", range_id)):
            if key in ocio_cicp and ocio_cicp[key] != actual:
                raise ToposColorMetadataError(f"OCIO/CICP conflict for {key}")
    return ToposColorSemantics(
        pixel_format=pixel_format, bit_depth=bit_depth,
        color_range=ColorRange.FULL if range_id == 1 else ColorRange.LIMITED,
        color_primaries=_TOPOS_PRIMARIES[primaries_id],
        color_transfer=_TOPOS_TRANSFERS[transfer_id],
        color_matrix=_TOPOS_MATRICES[matrix_id], chroma_siting=chroma_siting,
        display_authority=ColorMetadataAuthority.ICCP if icc_profile is not None
        else ColorMetadataAuthority.CICP, ocio_colorspace=ocio_value,
    )


def alpha_association(alpha_mode: int) -> AlphaAssociation:
    """Map the TPIM alpha field; unknown modes are hard errors."""
    try:
        return {0: AlphaAssociation.ABSENT, 1: AlphaAssociation.STRAIGHT,
                2: AlphaAssociation.PREMULTIPLIED}[alpha_mode]
    except KeyError as exc:
        raise ToposColorMetadataError(f"unknown alpha mode={alpha_mode}") from exc


def premultiply_alpha(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Premultiply RGB at the explicit display boundary, preserving shape."""
    if rgb.shape[:2] != alpha.shape[:2]:
        raise ValueError("RGB and alpha dimensions do not match")
    return rgb.astype(np.float32) * alpha.astype(np.float32)[..., None]


def unpremultiply_alpha(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Undo premultiplication without destroying RGB where alpha is zero."""
    if rgb.shape[:2] != alpha.shape[:2]:
        raise ValueError("RGB and alpha dimensions do not match")
    result = np.array(rgb, dtype=np.float32, copy=True)
    mask = alpha > 0.0
    result[mask] /= alpha[mask, None]
    return result


class ColorConverter:
    """
    色彩转换器（CPU 实现）。

    提供 YUV → RGB、range 转换、HDR tonemap 的 CPU fallback 实现。
    与 GPU shader 逻辑保持一致，用于非 GPU 路径或预览。

    支持的色彩矩阵：
    - BT.709: HD 视频（720p, 1080p, 4K SDR）
    - BT.2020 NCL: UHD/HDR 视频
    - BT.601: SD 视频（PAL/NTSC）

    支持的 range：
    - Limited: TV legal range (Y: 16-235, C: 16-240)
    - Full: PC/data range (0-255)
    """

    # =====================================================================
    # YUV → RGB Conversion Matrices
    # =====================================================================

    # BT.709 matrix (HD video, Rec. 709)
    # Y = 0.2126*R + 0.7152*G + 0.0722*B
    # RGB = [Y, U-0.5, V-0.5] * matrix
    BT709_MATRIX: np.ndarray = np.array([
        [1.0, 1.0, 1.0],
        [0.0, -0.1873, 1.8556],
        [1.5748, -0.4681, 0.0]
    ], dtype=np.float32)

    # BT.2020 NCL matrix (UHD/HDR, Rec. 2020 non-constant luminance)
    # Y = 0.2627*R + 0.6780*G + 0.0593*B
    BT2020_NCL_MATRIX: np.ndarray = np.array([
        [1.0, 1.0, 1.0],
        [0.0, -0.1646, 1.8814],
        [1.4746, -0.5714, 0.0]
    ], dtype=np.float32)

    # BT.601 NTSC matrix (SD video, SMPTE 170M)
    # Y = 0.299*R + 0.587*G + 0.114*B
    BT601_NTSC_MATRIX: np.ndarray = np.array([
        [1.0, 1.0, 1.0],
        [0.0, -0.3945, 2.032],
        [1.140, -0.581, 0.0]
    ], dtype=np.float32)

    # BT.601 PAL matrix (SD video, BT.470BG)
    # Y = 0.299*R + 0.587*G + 0.114*B (same as NTSC)
    BT601_PAL_MATRIX: np.ndarray = np.array([
        [1.0, 1.0, 1.0],
        [0.0, -0.3945, 2.032],
        [1.140, -0.581, 0.0]
    ], dtype=np.float32)

    # Matrix lookup table
    MATRIX_LUT: Dict[ColorMatrix, np.ndarray] = {
        ColorMatrix.BT709: BT709_MATRIX,
        ColorMatrix.BT2020_NC: BT2020_NCL_MATRIX,
        ColorMatrix.BT601: BT601_NTSC_MATRIX,
        ColorMatrix.BT601_PAL: BT601_PAL_MATRIX,
    }

    # =====================================================================
    # Range Conversion Constants
    # =====================================================================

    # Limited range (TV legal range)
    # Y: [16, 235] mapped to [0, 1]
    # C: [16, 240] mapped to [-0.5, 0.5]
    Y_OFFSET_LIMITED: float = 16.0 / 255.0
    Y_SCALE_LIMITED: float = 255.0 / 219.0  # 235 - 16 = 219
    C_OFFSET_LIMITED: float = 128.0 / 255.0
    C_SCALE_LIMITED: float = 255.0 / 224.0  # 240 - 16 = 224

    # Full range (PC range)
    # Y: [0, 255] mapped to [0, 1]
    # C: [0, 255] mapped to [-0.5, 0.5]
    C_OFFSET_FULL: float = 0.5

    # High bit depth constants (10-bit, 12-bit, 16-bit)
    # Limited range:
    # 10-bit: Y: [64, 940], C: [64, 960]
    # 12-bit: Y: [256, 3760], C: [256, 3840]
    # 16-bit: Y: [4096, 60160], C: [4096, 61440]
    RANGE_CONSTANTS: Dict[int, Dict[str, float]] = {
        8: {
            'y_offset': 16.0,
            'y_max': 235.0,
            'c_offset': 128.0,
            'c_max': 240.0,
            'scale': 255.0,
        },
        10: {
            'y_offset': 64.0,
            'y_max': 940.0,
            'c_offset': 512.0,
            'c_max': 960.0,
            'scale': 1023.0,
        },
        12: {
            'y_offset': 256.0,
            'y_max': 3760.0,
            'c_offset': 2048.0,
            'c_max': 3840.0,
            'scale': 4095.0,
        },
        16: {
            'y_offset': 4096.0,
            'y_max': 60160.0,
            'c_offset': 32768.0,
            'c_max': 61440.0,
            'scale': 65535.0,
        },
    }

    # =====================================================================
    # HDR Constants (PQ/HLG)
    # =====================================================================

    # PQ (SMPTE ST 2084) constants
    PQ_M1: float = 2610.0 / 4096.0 * 0.25  # 0.1593017578125
    PQ_M2: float = 2523.0 / 4096.0 * 128.0  # 78.84375
    PQ_C1: float = 3424.0 / 4096.0  # 0.8359375
    # ST 2084 constants are c2=2413/128 and c3=2392/128.  They are not
    # 12-bit code fractions; using the latter makes the denominator negative
    # at code value 1.0 and overflows the inverse EOTF.
    PQ_C2: float = 2413.0 / 128.0  # 18.8515625
    PQ_C3: float = 2392.0 / 128.0  # 18.6875
    PQ_LMAX: float = 10000.0  # Peak luminance (nits)

    # HLG (ARIB STD-B67) constants
    HLG_A: float = 0.17883277
    HLG_B: float = 0.28466892
    HLG_C: float = 0.55991073
    HLG_REF_WHITE: float = 100.0  # Reference white level (nits)

    def __init__(self) -> None:
        """初始化色彩转换器。"""
        pass

    def decode_transfer_to_linear(
        self, data: np.ndarray, transfer: ColorTransfer,
    ) -> np.ndarray:
        """Decode a transfer function without display tone mapping.

        PQ returns absolute nits (0..10000); HLG returns scene-relative
        linear values; SDR/linear transfers return normalized linear values.
        Keeping these domains explicit prevents a preview tone mapper from
        silently becoming part of codec color reconstruction.
        """
        values = np.asarray(data, dtype=np.float32)
        if transfer == ColorTransfer.PQ or transfer == ColorTransfer.ST2084:
            return self.pq_to_linear(values)
        if transfer == ColorTransfer.HLG:
            return self.hlg_to_linear(values)
        if transfer == ColorTransfer.SRGB:
            return self.srgb_to_linear(values)
        if transfer == ColorTransfer.BT709:
            # The project stores BT.709 video using the same display-referred
            # reference curve as the existing scalar CPU path.
            return self.srgb_to_linear(values)
        if transfer == ColorTransfer.LINEAR:
            return values
        if transfer == ColorTransfer.TRAW_LOG0:
            # RC7 词表统一（2026-09-21 复查补）：PWL-log12 逆映射——与
            # cfa debayer shader/CPU 链同源公式（cfa_cpu_debayer 常量）。
            # 输入为归一化 LOG0 码值 [0,1]（×4095 进曲线域），输出为
            # 满阱归一化线性 [0,1]（fw=16384）。此前显式拒绝——TRAW 标签
            # 帧进 ColorConverter 即异常，词表四点不闭环。
            from src.shared.video.cfa_cpu_debayer import (
                TRAW_CODE_MAX,
                TRAW_CURVE_FW_E,
                TRAW_CURVE_TOE_E,
                traw_log12_constants,
            )

            a, k, c0 = traw_log12_constants()
            codes = np.asarray(values, dtype=np.float64) * TRAW_CODE_MAX
            x = np.where(
                codes < a,
                (codes - c0) / k,
                TRAW_CURVE_TOE_E * (np.exp2(codes / a) - 1.0),
            )
            x = np.clip(x, 0.0, TRAW_CURVE_FW_E)
            return (x / TRAW_CURVE_FW_E).astype(np.float32)
        raise ToposColorMetadataError(f"unsupported transfer decode: {transfer.value}")

    def linear_to_display(
        self, linear: np.ndarray, transfer: ColorTransfer,
    ) -> np.ndarray:
        """Encode an explicit linear domain for a display transfer.

        For PQ the input is absolute nits; for HLG it is scene-relative.  No
        peak adaptation or tone mapping is performed here.  Callers must make
        that policy explicit before invoking this method.
        """
        values = np.asarray(linear, dtype=np.float32)
        if transfer == ColorTransfer.PQ or transfer == ColorTransfer.ST2084:
            return self.linear_to_pq(values)
        if transfer in (ColorTransfer.SRGB, ColorTransfer.BT709):
            return self.linear_to_srgb(values)
        if transfer == ColorTransfer.HLG:
            return self.linear_to_hlg(values)
        if transfer == ColorTransfer.LINEAR:
            return values
        raise ToposColorMetadataError(f"unsupported display transfer: {transfer.value}")

    def decode_to_display(
        self,
        data: np.ndarray,
        source_transfer: ColorTransfer,
        display_transfer: ColorTransfer = ColorTransfer.SRGB,
        *,
        pq_display_peak_nits: Optional[float] = None,
    ) -> np.ndarray:
        """Convert encoded samples to a display transfer explicitly.

        PQ-to-SDR requires ``pq_display_peak_nits`` because converting absolute
        nits to normalized SDR is a display policy.  The function performs only
        a documented normalization and transfer conversion; it does not hide
        a filmic/Reinhard tone mapper inside codec decode.
        """
        linear = self.decode_transfer_to_linear(data, source_transfer)
        if source_transfer in (ColorTransfer.PQ, ColorTransfer.ST2084) and display_transfer not in (
                ColorTransfer.PQ, ColorTransfer.ST2084):
            if pq_display_peak_nits is None or pq_display_peak_nits <= 0.0:
                raise ToposColorMetadataError(
                    "PQ to non-PQ display conversion requires pq_display_peak_nits")
            linear = linear / float(pq_display_peak_nits)
        return np.clip(self.linear_to_display(linear, display_transfer), 0.0, 1.0)

    # =====================================================================
    # YUV → RGB Conversion
    # =====================================================================

    def yuv_to_rgb(
        self,
        y: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        matrix: ColorMatrix,
        range_: ColorRange,
        bit_depth: int = 8
    ) -> np.ndarray:
        """
        YUV → RGB 转换（CPU 实现）。

        将 YUV/YCbCr 数据转换为 RGB，支持多种色彩矩阵和 range。

        Args:
            y: Y 平面数据（亮度），shape: (H, W)
            u: U 平面数据（色度），shape: (H/div_h, W/div_w)
            v: V 平面数据（色度），shape: (H/div_h, W/div_w)
            matrix: 色彩矩阵（BT.709, BT.2020, BT.601）
            range_: 色彩范围（LIMITED, FULL）
            bit_depth: 位深度（8, 10, 12, 16）

        Returns:
            RGB 数组，shape: (H, W, 3)，dtype: float32，范围 [0, 1]
            注意：HDR 数据可能超过 1.0，保留高光
        """
        # 获取矩阵
        # AUD-34402: 未收录矩阵不再静默回退 BT.709，补告警便于排查错色。
        if matrix not in self.MATRIX_LUT:
            logger.warning(
                "Unknown color matrix %s; falling back to BT.709 "
                "(colors may be wrong)", matrix,
            )
        conv_matrix = self.MATRIX_LUT.get(matrix, self.BT709_MATRIX)

        # 确保 U/V 与 Y 尺寸匹配（处理色度子采样）
        target_shape = y.shape[:2]
        if u.shape[:2] != target_shape:
            u = self._match_chroma_to_luma(u, target_shape)
        if v.shape[:2] != target_shape:
            v = self._match_chroma_to_luma(v, target_shape)

        # Range 转换
        y_norm, u_norm, v_norm = self._apply_range_conversion(
            y.astype(np.float32),
            u.astype(np.float32),
            v.astype(np.float32),
            range_,
            bit_depth
        )

        # YUV → RGB 转换
        # RGB = matrix * [Y, U, V]
        yuv_vec = np.stack([y_norm, u_norm, v_norm], axis=-1)  # (H, W, 3)
        rgb = np.dot(yuv_vec, conv_matrix)  # (H, W, 3)

        # 不 clamp（保留 HDR 高光）
        return rgb.astype(np.float32)

    @staticmethod
    def _match_chroma_to_luma(chroma: np.ndarray, target_shape: Tuple[int, int]) -> np.ndarray:
        """
        Resize a chroma plane to the luma plane size using nearest-neighbor repeat.

        FFmpeg/PyAV planar frames can have odd dimensions or chroma planes whose
        shapes do not divide the luma plane exactly.  Use ceil repeat, then crop
        or edge-pad so the result is always exactly target_shape.
        """
        target_h, target_w = target_shape
        if chroma.ndim < 2:
            raise ValueError(f"Chroma plane must be at least 2D, got shape={chroma.shape}")

        src_h, src_w = chroma.shape[:2]
        if src_h <= 0 or src_w <= 0 or target_h <= 0 or target_w <= 0:
            raise ValueError(f"Invalid chroma/luma shape: chroma={chroma.shape}, target={target_shape}")

        if (src_h, src_w) == (target_h, target_w):
            return chroma

        scale_h = max(1, (target_h + src_h - 1) // src_h)
        scale_w = max(1, (target_w + src_w - 1) // src_w)
        resized = np.repeat(np.repeat(chroma, scale_h, axis=0), scale_w, axis=1)

        pad_h = max(0, target_h - resized.shape[0])
        pad_w = max(0, target_w - resized.shape[1])
        if pad_h or pad_w:
            pad_spec = [(0, pad_h), (0, pad_w)]
            pad_spec.extend((0, 0) for _ in range(resized.ndim - 2))
            resized = np.pad(resized, pad_spec, mode='edge')

        return resized[:target_h, :target_w]

    def _apply_range_conversion(
        self,
        y: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        range_: ColorRange,
        bit_depth: int
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        应用 range 转换。

        Args:
            y, u, v: YUV 原始数据（float32）
            range_: 色彩范围
            bit_depth: 位深度

        Returns:
            (y_norm, u_norm, v_norm) 规范化后的数据
        """
        # 获取位深度常数
        constants = self.RANGE_CONSTANTS.get(bit_depth, self.RANGE_CONSTANTS[8])
        scale = constants['scale']

        if range_ == ColorRange.LIMITED:
            # Limited range (TV legal)
            y_offset = constants['y_offset']
            y_max = constants['y_max']
            c_offset = constants['c_offset']
            c_max = constants['c_max']
            y_range = y_max - y_offset  # e.g. 219 for 8-bit, 876 for 10-bit
            # 色度全幅：[c_min, c_max] = 8-bit [16,240] 跨度 224（半幅 112
            # 的两倍，中心 c_offset 对称）——归一化到 [-0.5, 0.5] 必须除
            # 以全幅。此前误用亮度跨度 219（压缩 ~2.3%）。
            c_range = 2.0 * (c_max - c_offset)  # e.g. 224 for 8-bit, 896 for 10-bit

            # Y: [offset, max] -> [0, 1]
            y_norm = (y - y_offset) / y_range

            # U/V: [offset centered] -> [-0.5, 0.5]
            # P0-COLOR-06：色度必须除以色度跨度（224/896），此前误用亮度
            # 跨度（219/876）——CPU 解码色度被压缩 ~2.3%（红/蓝饱和色
            # 偏差 10+ LSB，10-bit 尤其明显）。GPU shader（yuv_gpu_upload）
            # 一直用 224 基准，此处对齐。
            u_norm = (u - c_offset) / c_range
            v_norm = (v - c_offset) / c_range

        else:
            # Full range (PC)
            # Y: [0, scale] -> [0, 1]
            y_norm = y / scale

            # U/V: [0, scale] -> [-0.5, 0.5]
            u_norm = (u / scale) - self.C_OFFSET_FULL
            v_norm = (v / scale) - self.C_OFFSET_FULL

        return y_norm, u_norm, v_norm

    # =====================================================================
    # Range Conversion Utilities
    # =====================================================================

    def limited_to_full(
        self,
        data: np.ndarray,
        is_luma: bool = True,
        bit_depth: int = 8
    ) -> np.ndarray:
        """
        Limited range → Full range 转换。

        将 TV legal range 数据转换为 PC full range。

        Args:
            data: 输入数据（任意 shape）
            is_luma: 是否为亮度数据（Y 平面）
                True: Y range [16-235] -> [0-255]
                False: C range [16-240] -> [0-255]
            bit_depth: 位深度（8, 10, 12, 16）

        Returns:
            Full range 数据，dtype 与输入相同
        """
        constants = self.RANGE_CONSTANTS.get(bit_depth, self.RANGE_CONSTANTS[8])
        scale = constants['scale']

        if is_luma:
            offset = constants['y_offset']
            max_val = constants['y_max']
        else:
            # AUD-34401：色度 limited 区间以 c_offset 为中心对称，
            # 下界 c_min = 2*c_offset - c_max（8-bit: 2*128-240 = 16）。
            # 此前误用 c_offset - c_max/2（"半幅"，8-bit 得 8），
            # 中性色度 128 被映射到 ≈131.9（正确 ≈127.5），整体色偏
            # 4–9 LSB。与 _apply_range_conversion 的 224 全幅基准一致。
            offset = 2.0 * constants['c_offset'] - constants['c_max']
            max_val = constants['c_max']

        # Linear mapping: limited -> full
        # full = (limited - offset) * scale / (max - offset)
        data_float = data.astype(np.float32)
        full_range = (data_float - offset) * scale / (max_val - offset)

        # Clamp to valid range
        full_range = np.clip(full_range, 0.0, scale)

        # 恢复原始 dtype
        if data.dtype == np.uint8:
            return full_range.astype(np.uint8)
        elif data.dtype == np.uint16:
            return full_range.astype(np.uint16)
        else:
            return full_range.astype(data.dtype)

    def full_to_limited(
        self,
        data: np.ndarray,
        is_luma: bool = True,
        bit_depth: int = 8
    ) -> np.ndarray:
        """
        Full range → Limited range 转换。

        将 PC full range 数据转换为 TV legal range。

        Args:
            data: 输入数据（任意 shape）
            is_luma: 是否为亮度数据
            bit_depth: 位深度

        Returns:
            Limited range 数据，dtype 与输入相同
        """
        constants = self.RANGE_CONSTANTS.get(bit_depth, self.RANGE_CONSTANTS[8])
        scale = constants['scale']

        if is_luma:
            offset = constants['y_offset']
            max_val = constants['y_max']
        else:
            # AUD-34401：与 limited_to_full 对称的下界 c_min（见上）
            offset = 2.0 * constants['c_offset'] - constants['c_max']
            max_val = constants['c_max']

        # Linear mapping: full -> limited
        # limited = full * (max - offset) / scale + offset
        data_float = data.astype(np.float32)
        limited_range = data_float * (max_val - offset) / scale + offset

        # Clamp to valid limited range
        limited_range = np.clip(limited_range, offset, max_val)

        # 恢复原始 dtype
        if data.dtype == np.uint8:
            return limited_range.astype(np.uint8)
        elif data.dtype == np.uint16:
            return limited_range.astype(np.uint16)
        else:
            return limited_range.astype(data.dtype)

    # =====================================================================
    # Transfer Function Conversion (HDR)
    # =====================================================================

    def pq_to_linear(self, data: np.ndarray) -> np.ndarray:
        """
        PQ (SMPTE ST 2084) → Linear 转换。

        将 PQ encoded HDR 数据转换为线性光值。
        PQ 设计峰值亮度为 10,000 nits。

        Args:
            data: PQ encoded 数据，范围 [0, 1]

        Returns:
            线性光数据，范围 [0, 10000/参考值] nits
        """
        # PQ inverse EOTF
        # E = ((max(E', 0)^(1/m2) - c1) / (c2 - c3 * max(E', 0)^(1/m2)))^(1/m1)

        data = np.clip(data, 0.0, 1.0)

        # Step 1: E'^(1/m2)
        v = np.power(data, 1.0 / self.PQ_M2)

        # Step 2: Apply inverse formula
        numerator = np.maximum(v - self.PQ_C1, 0.0)
        denominator = np.maximum(self.PQ_C2 - self.PQ_C3 * v, 1e-10)

        # Step 3: ^(1/m1)
        linear = np.power(numerator / denominator, 1.0 / self.PQ_M1)

        # Scale to nits (10,000 peak)
        linear = linear * self.PQ_LMAX

        return linear.astype(np.float32)

    def linear_to_pq(self, data: np.ndarray) -> np.ndarray:
        """
        Linear → PQ 转换。

        将线性光值转换为 PQ encoded HDR 数据。

        Args:
            data: 线性光数据（nits）

        Returns:
            PQ encoded 数据，范围 [0, 1]
        """
        # Normalize to 10,000 nits
        normalized = np.clip(data / self.PQ_LMAX, 0.0, 1.0)

        # PQ forward EOTF
        # E' = ((c1 + c2 * E^(m1)) / (1 + c3 * E^(m1)))^(m2)

        # Step 1: E^(m1)
        v = np.power(normalized, self.PQ_M1)

        # Step 2: Apply formula
        numerator = self.PQ_C1 + self.PQ_C2 * v
        denominator = 1.0 + self.PQ_C3 * v

        # Step 3: ^(m2)
        pq = np.power(numerator / denominator, self.PQ_M2)

        return pq.astype(np.float32)

    def hlg_to_linear(self, data: np.ndarray) -> np.ndarray:
        """
        HLG (ARIB STD-B67) → Linear 转换。

        HLG 是广播 HDR 标准，兼容 SDR 显示器。

        Args:
            data: HLG encoded 数据，范围 [0, 1]

        Returns:
            线性光数据（相对值）
        """
        data = np.clip(data, 0.0, 1.0)

        # HLG inverse OETF
        # For E' <= 0.5: E = (E'^2) / 3
        # For E' > 0.5: E = exp((E' - c) / a) + b

        # 使用分段函数
        low_region = data <= 0.5
        high_region = data > 0.5

        linear = np.zeros_like(data)

        # Low region: simple square
        linear[low_region] = np.square(data[low_region]) / 3.0

        # High region: exponential
        linear[high_region] = np.exp((data[high_region] - self.HLG_C) / self.HLG_A) + self.HLG_B

        return linear.astype(np.float32)

    def linear_to_hlg(self, data: np.ndarray) -> np.ndarray:
        """
        Linear → HLG 转换。

        Args:
            data: 线性光数据

        Returns:
            HLG encoded 数据，范围 [0, 1]
        """
        # HLG forward OETF
        # For E <= 1: E' = sqrt(3 * E)
        # For E > 1: E' = a * ln(E - b) + c

        low_region = data <= 1.0
        high_region = data > 1.0

        hlg = np.zeros_like(data)

        # Low region: sqrt
        hlg[low_region] = np.sqrt(3.0 * data[low_region])

        # High region: log
        hlg[high_region] = self.HLG_A * np.log(data[high_region] - self.HLG_B) + self.HLG_C

        return np.clip(hlg, 0.0, 1.0).astype(np.float32)

    def srgb_to_linear(self, data: np.ndarray) -> np.ndarray:
        """
        sRGB → Linear 转换。

        Args:
            data: sRGB encoded 数据，范围 [0, 1]

        Returns:
            线性光数据，范围 [0, 1]
        """
        data = np.clip(data, 0.0, 1.0)

        # sRGB inverse EOTF (piecewise)
        low_region = data <= 0.04045
        high_region = data > 0.04045

        linear = np.zeros_like(data)

        # Low region: linear
        linear[low_region] = data[low_region] / 12.92

        # High region: gamma 2.4
        linear[high_region] = np.power((data[high_region] + 0.055) / 1.055, 2.4)

        return linear.astype(np.float32)

    def linear_to_srgb(self, data: np.ndarray) -> np.ndarray:
        """
        Linear → sRGB 转换。

        Args:
            data: 线性光数据，范围 [0, 1]

        Returns:
            sRGB encoded 数据，范围 [0, 1]
        """
        data = np.clip(data, 0.0, 1.0)

        # sRGB forward EOTF (piecewise)
        low_region = data <= 0.0031308
        high_region = data > 0.0031308

        srgb = np.zeros_like(data)

        # Low region: linear
        srgb[low_region] = data[low_region] * 12.92

        # High region: gamma 1/2.4
        srgb[high_region] = 1.055 * np.power(data[high_region], 1.0 / 2.4) - 0.055

        return srgb.astype(np.float32)

    # =====================================================================
    # Tone Mapping (HDR → SDR)
    # =====================================================================

    def tonemap_pq_to_srgb(
        self,
        data: np.ndarray,
        target_peak: float = 100.0,
        gamma: float = 2.2
    ) -> np.ndarray:
        """
        PQ HDR → sRGB SDR tone mapping。

        简化的 HDR tone mapping，用于预览。

        Args:
            data: PQ encoded HDR 数据，范围 [0, 1]
            target_peak: 目标峰值亮度（nits），默认 100
            gamma: 目标 gamma，默认 2.2

        Returns:
            sRGB encoded SDR 数据，范围 [0, 1]
        """
        # PQ → Linear (nits)
        linear_nits = self.pq_to_linear(data)

        # Scale to target range
        # 简化的 Reinhard tone mapping: L_out = L_in / (1 + L_in)
        # 或使用更保守的映射

        # Normalize and tone map
        normalized = linear_nits / target_peak
        mapped = normalized / (1.0 + normalized)

        # Apply gamma for sRGB-like output
        output = np.power(mapped, 1.0 / gamma)

        return np.clip(output, 0.0, 1.0).astype(np.float32)

    # =====================================================================
    # Utility Methods
    # =====================================================================

    @staticmethod
    def get_matrix_coefficients(matrix: ColorMatrix) -> Tuple[Tuple[float, float, float], ...]:
        """
        获取色彩矩阵系数（用于 shader uniform）。

        Args:
            matrix: 色彩矩阵

        Returns:
            ((Kr, Kg, Kb), (Ku0, Ku1, Ku2), (Kv0, Kv1, Kvv2))
            其中 Y = Kr*R + Kg*G + Kb*B
        """
        coefficients: Dict[ColorMatrix, Tuple[Tuple[float, float, float], ...]] = {
            ColorMatrix.BT709: ((0.2126, 0.7152, 0.0722), (0.0, -0.1873, 1.8556), (1.5748, -0.4681, 0.0)),
            ColorMatrix.BT2020_NC: ((0.2627, 0.6780, 0.0593), (0.0, -0.1646, 1.8814), (1.4746, -0.5714, 0.0)),
            ColorMatrix.BT601: ((0.299, 0.587, 0.114), (0.0, -0.3945, 2.032), (1.140, -0.581, 0.0)),
            ColorMatrix.BT601_PAL: ((0.299, 0.587, 0.114), (0.0, -0.3945, 2.032), (1.140, -0.581, 0.0)),
        }
        # AUD-34402: 未收录矩阵回退不再静默。
        if matrix not in coefficients:
            logger.warning(
                "Unknown color matrix %s for shader coefficients; "
                "falling back to BT.709", matrix,
            )
        return coefficients.get(matrix, coefficients[ColorMatrix.BT709])

    @staticmethod
    def get_shader_matrix_id(matrix: ColorMatrix) -> int:
        """
        获取 shader 使用的矩阵 ID。

        Args:
            matrix: 色彩矩阵

        Returns:
            整数 ID（0=BT709, 1=BT2020, 2=BT601）
        """
        id_map: Dict[ColorMatrix, int] = {
            ColorMatrix.BT709: 0,
            ColorMatrix.BT2020_NC: 1,
            ColorMatrix.BT601: 2,
            ColorMatrix.BT601_PAL: 2,
        }
        # AUD-34402: 未收录矩阵回退不再静默。
        if matrix not in id_map:
            logger.warning(
                "Unknown color matrix %s for shader id; falling back to 0 "
                "(BT.709)", matrix,
            )
        return id_map.get(matrix, 0)


# =============================================================================
# Module-level singleton accessor
# =============================================================================

_converter_instance: Optional[ColorConverter] = None


def get_color_converter() -> ColorConverter:
    """
    获取 ColorConverter 单例实例。

    Returns:
        ColorConverter 实例
    """
    global _converter_instance
    if _converter_instance is None:
        _converter_instance = ColorConverter()
    return _converter_instance
