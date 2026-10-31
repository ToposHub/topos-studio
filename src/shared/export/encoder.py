"""
Video Encoder Configuration Module

Defines base configuration classes for video encoding.
Provides FFmpeg-compatible codec settings for professional video export.

## Supported Codecs

- ProRes: Apple ProRes family (Proxy, LT, 422, HQ, 4444, 4444 XQ)
- DNxHR: Avid DNxHR family (LB, SQ, HQ, HQX, 444)
- H.264/H.265: General purpose codecs
- FFV1: Lossless archival codec

## Design Principles

1. Immutable configuration: frozen=True for thread safety
2. Complete type annotations: all methods have type hints
3. FFmpeg compatible: settings map directly to FFmpeg parameters
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:  # AUD-33119: annotation-only import (no runtime cycle risk)
    from src.shared.media import Rational


class VideoCodec(Enum):
    """Video codec identifiers."""
    PRORES = 'prores'
    DNXHR = 'dnxhr'
    H264 = 'h264'
    H265 = 'hevc'
    FFV1 = 'ffv1'
    VP9 = 'vp9'
    AV1 = 'av1'
    GIF = 'gif'


class ContainerFormat(Enum):
    """Container format identifiers."""
    MOV = 'mov'
    MXF = 'mxf'
    MP4 = 'mp4'
    MKV = 'mkv'
    AVI = 'avi'
    WEBM = 'webm'
    GIF = 'gif'


class ColorSpace(Enum):
    """Color space identifiers for video metadata."""
    BT709 = 'bt709'
    BT2020_NCL = 'bt2020nc'
    BT2020_CL = 'bt2020c'
    SMPTE170M = 'smpte170m'
    SMPTE240M = 'smpte240m'
    FCC = 'fcc'
    BT470BG = 'bt470bg'
    UNSPECIFIED = 'unspecified'


class ColorTransfer(Enum):
    """Color transfer function (gamma/EOTF) identifiers."""
    BT709 = 'bt709'
    BT1886 = 'bt1886'
    SMPTE2084 = 'smpte2084'  # PQ (HDR)
    HLG = 'arib-std-b67'     # HLG (HDR)
    LINEAR = 'linear'
    SMPTE170M = 'smpte170m'
    UNSPECIFIED = 'unspecified'


class ColorPrimaries(Enum):
    """Color primaries identifiers."""
    BT709 = 'bt709'
    BT2020 = 'bt2020'
    SMPTE170M = 'smpte170m'
    SMPTE240M = 'smpte240m'
    BT470BG = 'bt470bg'
    BT470M = 'bt470m'
    FILM = 'film'
    UNSPECIFIED = 'unspecified'


@dataclass(frozen=True)
class VideoEncoderConfig:
    """
    Video encoder configuration.

    Contains all parameters needed to configure FFmpeg video encoding.
    Designed for professional video workflows (NLE compatible output).

    Attributes:
        codec: Video codec identifier
        profile: Codec profile (e.g., 'proxy', 'hq', 'high', 'main')
        pix_fmt: FFmpeg pixel format name (e.g., 'yuv422p10le', 'yuva444p10le')
        bit_depth: Bits per color channel (8, 10, 12, 16)
        has_alpha: Whether the format supports alpha channel
        target_bitrate: Target bitrate in Mbps (optional, codec-specific)
        max_bitrate: Maximum bitrate in Mbps (optional, for constrained encoding)
        container: Container format (mov, mxf, mp4, mkv)
        color_space: Color space metadata
        color_transfer: Color transfer function metadata
        color_primaries: Color primaries metadata
        quality: Quality setting for configurable codecs (0-100)
        preset: Encoding preset for H.264/H.265 (ultrafast, fast, medium, slow, veryslow)
        threads: Number of encoding threads (0 = auto)
        hwaccel: Hardware acceleration method (none, nvenc, qsv, videotoolbox)
        extra_options: Additional FFmpeg options
    """

    codec: str
    profile: Optional[str] = None
    pix_fmt: str = 'yuv422p'
    bit_depth: int = 8
    has_alpha: bool = False
    target_bitrate: Optional[int] = None  # Mbps
    max_bitrate: Optional[int] = None      # Mbps
    container: str = 'mov'
    color_space: Optional[ColorSpace] = None
    color_transfer: Optional[ColorTransfer] = None
    color_primaries: Optional[ColorPrimaries] = None
    quality: Optional[int] = None          # 0-100
    preset: Optional[str] = None           # H.264/H.265 preset
    threads: int = 0                       # 0 = auto
    hwaccel: Optional[str] = None          # nvenc, qsv, videotoolbox
    extra_options: Dict[str, Any] = field(default_factory=dict)

    def get_ffmpeg_codec_name(self) -> str:
        """
        Get FFmpeg codec name for this configuration.

        Returns:
            FFmpeg codec identifier string
        """
        codec_map = {
            'prores': 'prores_ks',
            'dnxhr': 'dnxhd',  # FFmpeg 无 'dnxhr' 编码器，DNxHR/DNxHD 均为 dnxhd
            'h264': 'libx264',
            'hevc': 'libx265',
            'ffv1': 'ffv1',
            'vp9': 'libvpx-vp9',
            'av1': 'libaom-av1',
        }
        return codec_map.get(self.codec, self.codec)

    def get_ffmpeg_pix_fmt(self) -> str:
        """
        Get FFmpeg pixel format string.

        Returns:
            FFmpeg pixel format identifier
        """
        return self.pix_fmt

    def get_ffmpeg_options(self) -> Dict[str, str]:
        """
        Build FFmpeg command-line options from configuration.

        Returns:
            Dictionary of FFmpeg option name to value
        """
        options: Dict[str, str] = {}

        # Profile
        if self.profile:
            options['profile:v'] = self.profile

        # Bitrate (for rate-controlled codecs)
        if self.target_bitrate:
            options['b:v'] = f'{self.target_bitrate}M'
        if self.max_bitrate:
            options['maxrate:v'] = f'{self.max_bitrate}M'
            options['bufsize:v'] = f'{self.max_bitrate * 2}M'

        # Quality (CRF/QP mode)
        if self.quality is not None:
            if self.codec in ('h264', 'hevc'):
                options['crf'] = str(self.quality)
            elif self.codec == 'vp9':
                options['crf'] = str(self.quality)
            elif self.codec == 'av1':
                options['crf'] = str(self.quality)

        # Preset (encoding speed/quality tradeoff)
        if self.preset:
            options['preset'] = self.preset

        # Threads
        if self.threads > 0:
            options['threads'] = str(self.threads)

        # Pixel format
        options['pix_fmt'] = self.pix_fmt

        # Color metadata
        if self.color_space:
            options['colorspace'] = self.color_space.value
        if self.color_transfer:
            options['color_trc'] = self.color_transfer.value
        if self.color_primaries:
            options['color_primaries'] = self.color_primaries.value

        # Extra options
        for key, value in self.extra_options.items():
            options[key] = str(value)

        return options

    def get_file_extension(self) -> str:
        """
        Get recommended file extension for this configuration.

        Returns:
            File extension string (e.g., '.mov', '.mxf', '.mp4')
        """
        ext_map = {
            'mov': '.mov',
            'mxf': '.mxf',
            'mp4': '.mp4',
            'mkv': '.mkv',
            'avi': '.avi',
            'webm': '.webm',
            'gif': '.gif',
        }
        return ext_map.get(self.container, '.mov')

    def is_hdr_capable(self) -> bool:
        """
        Check if this configuration supports HDR output.

        Returns:
            True if HDR metadata or 10+ bit depth configured
        """
        return (
            self.bit_depth >= 10 or
            self.color_transfer in (ColorTransfer.SMPTE2084, ColorTransfer.HLG) or
            self.color_primaries == ColorPrimaries.BT2020
        )

    def is_nle_compatible(self) -> bool:
        """
        Check if output is NLE (Non-Linear Editor) compatible.

        Returns:
            True if format is standard for professional video workflows
        """
        # ProRes and DNxHR are universally NLE compatible
        if self.codec in ('prores', 'dnxhr'):
            return True

        # MOV container with standard codec is generally compatible
        if self.container == 'mov' and self.codec in ('h264', 'hevc'):
            return self.profile in ('high', 'main')

        return False

    def get_description(self) -> str:
        """
        Get human-readable description of this configuration.

        Returns:
            Description string
        """
        parts = [self.codec.upper()]

        if self.profile:
            parts.append(self.profile.upper())

        if self.bit_depth > 8:
            parts.append(f'{self.bit_depth}-bit')

        if self.has_alpha:
            parts.append('+Alpha')

        if self.target_bitrate:
            parts.append(f'~{self.target_bitrate}Mbps')

        return ' '.join(parts)


# =============================================================================
# W0-04: 硬件编码器映射与能力查询（NVENC/QSV/AMF/VideoToolbox 接缝）
#
# 映射层只回答「(hwaccel, codec) → FFmpeg 编码器名」与「编码器注册存在」；
# 「注册存在 ≠ 驱动可用」（AMD AMF 实测教训，见 scripts/
# BENCH_HW_DECODE_WINDOWS.md）——真正的可用裁决在编码会话 open() 时
# 单帧探针（见 video_encoder.py 的 AMF 探针先例），失败回退 CPU 并
# 记录 encoder_hw_unavailable:<hwaccel>。
# =============================================================================

# (hwaccel, codec) → FFmpeg 编码器名。缺组合 = 该 hwaccel 不支持该 codec。
# 'h265' 是 'hevc' 的项目内活跃别名（delivery_profile/proxy/seek 等在用），
# 每个 hwaccel 必须同等对待两个拼写——否则同一 codec 串会在不同 hwaccel
# 下一侧硬编、一侧静默回退 CPU。
HWACCEL_ENCODER_NAME_MAP: Dict[tuple[str, str], str] = {
    ('nvenc', 'h264'): 'h264_nvenc',
    ('nvenc', 'hevc'): 'hevc_nvenc',
    ('nvenc', 'h265'): 'hevc_nvenc',
    ('nvenc', 'av1'): 'av1_nvenc',          # Turing+ (NVENC AV1)
    ('qsv', 'h264'): 'h264_qsv',
    ('qsv', 'hevc'): 'hevc_qsv',
    ('qsv', 'h265'): 'hevc_qsv',
    ('qsv', 'av1'): 'av1_qsv',
    ('amf', 'h264'): 'h264_amf',
    ('amf', 'hevc'): 'hevc_amf',
    ('amf', 'h265'): 'hevc_amf',
    ('amf', 'av1'): 'av1_amf',              # RDNA3+/较新 FFmpeg 才注册
    ('videotoolbox', 'h264'): 'h264_videotoolbox',
    ('videotoolbox', 'hevc'): 'hevc_videotoolbox',
    ('videotoolbox', 'h265'): 'hevc_videotoolbox',
}

_KNOWN_HWACCELS = frozenset({
    'nvenc', 'qsv', 'amf', 'videotoolbox', 'none',
})

_probe_cache: Dict[str, bool] = {}


def hwaccel_encoder_name(hwaccel: str, codec: str) -> Optional[str]:
    """(hwaccel, codec) → FFmpeg 编码器名；hwaccel 未知抛 ValueError。

    已知 hwaccel 但无该 codec 组合（如 nvenc+prores）返回 None——
    调用方应回退 CPU 并记录诊断，不得静默换名。
    """
    name = (hwaccel or '').strip().lower()
    if name in ('', 'none'):
        return None
    if name not in _KNOWN_HWACCELS:
        valid = ', '.join(sorted(_KNOWN_HWACCELS - {'none'}))
        raise ValueError(f"invalid hwaccel {hwaccel!r}; expected one of: {valid}")
    codec_lower = (codec or '').strip().lower()
    return HWACCEL_ENCODER_NAME_MAP.get((name, codec_lower))


def probe_hwaccel_encoder_available(encoder_name: str) -> bool:
    """FFmpeg 是否注册了该编码器（带缓存；注册存在 ≠ 驱动可用）。"""
    if not encoder_name:
        return False
    if encoder_name not in _probe_cache:
        try:
            import av

            av.codec.Codec(encoder_name, 'w')
            _probe_cache[encoder_name] = True
        except Exception:
            _probe_cache[encoder_name] = False
    return _probe_cache[encoder_name]


def hwaccel_unavailable_reason(hwaccel: str) -> str:
    """显式诊断 reason（计划 §4：encoder_hw_unavailable:<hwaccel>）。"""
    from src.shared.media.windows_hw.reasons import FallbackReason

    return f"{FallbackReason.ENCODER_HW_UNAVAILABLE.value}:{hwaccel}"


@dataclass(frozen=True)
class AudioEncoderConfig:
    """
    Audio encoder configuration.

    Attributes:
        codec: Audio codec (aac, pcm_s16le, pcm_s24le, pcm_f32le, mp3, flac)
        bitrate: Target bitrate in kbps (for compressed codecs)
        sample_rate: Sample rate in Hz (48000, 44100, etc.)
        channels: Number of audio channels
        bit_depth: Bits per sample (for PCM codecs)
    """

    codec: str = 'aac'
    bitrate: Optional[int] = None  # kbps
    sample_rate: int = 48000
    channels: int = 2
    bit_depth: Optional[int] = None  # For PCM

    def get_ffmpeg_codec_name(self) -> str:
        """Get FFmpeg codec name."""
        codec_map = {
            'aac': 'aac',
            'pcm_s16le': 'pcm_s16le',
            'pcm_s24le': 'pcm_s24le',
            'pcm_f32le': 'pcm_f32le',
            'mp3': 'libmp3lame',
            'flac': 'flac',
        }
        return codec_map.get(self.codec, self.codec)

    def get_ffmpeg_options(self) -> Dict[str, str]:
        """Build FFmpeg audio options."""
        options: Dict[str, str] = {}

        if self.bitrate:
            options['b:a'] = f'{self.bitrate}k'

        options['ar'] = str(self.sample_rate)
        options['ac'] = str(self.channels)

        return options


@dataclass(frozen=True)
class ExportConfig:
    """
    Complete export configuration (video + audio).

    Frozen (design principle 1: "Immutable configuration: frozen=True for
    thread safety", AUD-33119) — construct a new instance to change settings.

    Attributes:
        video: Video encoder configuration
        audio: Audio encoder configuration
        output_path: Output file path
        start_frame: First frame to export (0-indexed)
        end_frame: Last frame to export (inclusive)
        framerate: Output framerate (media Rational)
        resolution: Output resolution (width, height)
        metadata: Additional file metadata
    """

    video: VideoEncoderConfig
    audio: Optional[AudioEncoderConfig] = None
    output_path: str = ''
    start_frame: int = 0
    end_frame: int = -1  # -1 = all frames
    framerate: Optional['Rational'] = None  # Rational from media module
    resolution: Optional[tuple[int, int]] = None
    metadata: Dict[str, str] = field(default_factory=dict)

    def get_total_frames(self, default_total: int) -> int:
        """
        Calculate total frames to export.

        Args:
            default_total: Default total frames if end_frame is -1

        Returns:
            Number of frames to export (never negative — an inverted or empty
            range yields 0 instead of a negative count, AUD-33119)
        """
        end = self.end_frame if self.end_frame >= 0 else default_total - 1
        return max(0, end - self.start_frame + 1)
