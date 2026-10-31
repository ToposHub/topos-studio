"""
Export Module for Topos Color

Provides video encoder configurations, presets, image sequence export,
and PyAV-based video encoding.

## Module Structure

- encoder: Base encoder configuration classes (VideoEncoderConfig)
- presets: ProRes, DNxHR, H.264, H.265 presets
- sequence_exporter: Image sequence export with HDR support
- frame_provider: ExportFrameProvider - deterministic frame iteration (P8.1)
- video_encoder: FFmpegVideoEncoder - PyAV encoder implementation (P8.2)

## Supported Video Formats

### ProRes (Apple)
- Proxy, LT, 422, HQ, 4444, 4444 XQ
- MOV container
- NLE compatible

### DNxHR (Avid)
- LB, SQ, HQ, HQX, 444
- MXF container (Avid standard)
- NLE compatible

### H.264/H.265
- Broadcast presets with bitrate control
- HDR presets (HDR10, HLG)
- MP4 container

### FFV1 (Lossless)
- Archival encoding
- MKV container
- Maximum quality preservation

## Image Sequence Formats

### EXR (OpenEXR)
- Full HDR support
- Float16/Float32 precision
- Scene-linear color space
- Alpha channel support

### TIFF
- 8/16/32-bit support
- HDR capable (16+ bit)
- LZW compression

### PNG
- 8-bit standard
- 16-bit extended
- sRGB color space

## Usage Examples

```python
# ProRes HQ export using presets
from src.shared.export import get_prores_config, ProResProfile

config = get_prores_config(ProResProfile.HQ)
print(config.get_ffmpeg_options())
# {'profile:v': 'hq', 'pix_fmt': 'yuv422p10le', 'b:v': '220M', ...}

# DNxHR 444 export
from src.shared.export import get_dnxhr_config, DNxHRProfile

config = get_dnxhr_config(DNxHRProfile.444)
print(config.container)  # 'mxf'

# Image sequence export
from src.shared.export import ImageSequenceExporter, create_sequence_exporter

exporter = create_sequence_exporter('/output', format='exr', use_case='vfx')
for i, frame in enumerate(frames):
    path = exporter.export_frame(frame, i)

# Recommended preset for use case
from src.shared.export import get_recommended_preset

config = get_recommended_preset('vfx', has_alpha=True)

# P8.1 + P8.2: Export video using ExportFrameProvider + FFmpegVideoEncoder
from src.shared.export import ExportFrameProvider, FFmpegVideoEncoder, FFmpegEncoderConfig
from src.shared.media import FFmpegMediaSource, Rational

# Create frame provider (deterministic, no frame drops)
source = FFmpegMediaSource('/path/to/input.mp4')
provider = ExportFrameProvider(source, start_frame=0, end_frame=100)

# Create encoder config
encoder_config = FFmpegEncoderConfig(
    codec='prores',
    width=1920,
    height=1080,
    fps=Rational(24, 1),
    profile='hq'
)

# Export video
encoder = FFmpegVideoEncoder('/path/to/output.mov', encoder_config)
encoder.open()

for frame in provider:
    encoder.encode_frame(frame)

encoder.close()
```

## NLE Compatibility

ProRes and DNxHR outputs are universally compatible with:
- DaVinci Resolve
- Adobe Premiere Pro
- Final Cut Pro
- Avid Media Composer
- After Effects
- Nuke
"""

from .encoder import (
    VideoCodec,
    ContainerFormat,
    ColorSpace,
    ColorTransfer,
    ColorPrimaries,
    VideoEncoderConfig,
    AudioEncoderConfig,
    ExportConfig,
)

from .presets import (
    ProResProfile,
    DNxHRProfile,
    H264Profile,
    H265Profile,
    PRORES_PRESETS,
    DNXHR_PRESETS,
    H264_BROADCAST_PRESETS,
    H265_HDR_PRESETS,
    ARCHIVAL_PRESETS,
    get_prores_config,
    get_dnxhr_config,
    get_prores_profile_for_bitrate,
    get_dnxhr_profile_for_resolution,
    estimate_prores_bitrate,
    is_alpha_required,
    get_all_presets,
    get_recommended_preset,
)

from .sequence_exporter import (
    ImageFormat,
    ColorPolicy,
    BitDepthMode,
    SequenceExportConfig,
    ImageSequenceExporter,
    create_sequence_exporter,
)

from .frame_provider import (
    ExportFrameProvider,
    ExportRange,
    create_export_provider,
    estimate_export_duration,
)

from .video_encoder import (
    FFmpegEncoderConfig,
    ColorMetadataConfig,
    FFmpegVideoEncoder,
    CodecType,
    PIXEL_FORMAT_MAP,
    ENCODER_NAME_MAP,
    create_encoder_config,
    get_recommended_codec,
)
from .gif_encoder import GifVideoEncoder

# P8-Topos：自研编码器（阶段 8）；六档 profile capability schema 位于
# src/shared/codec/topos_profiles.py（中立层，media 代理链共用），此处重导出
from .topos_encoder import ToposVideoEncoder
from .render_cache import DiskRenderCache, RenderCacheStats, SegmentRenderKey
from src.shared.codec.topos_profiles import (
    TOPOS_PROFILE_TIERS,
    AVAILABLE_TOPOS_TIERS,
    ToposProfileTier,
    available_topos_tiers,
    default_topos_tier_id,
    get_topos_tier,
    get_topos_tier_or_none,
    topos_capability_summary,
)


# Public exports
__all__ = [
    # Encoder types (from encoder.py)
    'VideoCodec',
    'ContainerFormat',
    'ColorSpace',
    'ColorTransfer',
    'ColorPrimaries',
    'VideoEncoderConfig',
    'AudioEncoderConfig',
    'ExportConfig',

    # Preset types (from presets.py)
    'ProResProfile',
    'DNxHRProfile',
    'H264Profile',
    'H265Profile',

    # Preset dictionaries
    'PRORES_PRESETS',
    'DNXHR_PRESETS',
    'H264_BROADCAST_PRESETS',
    'H265_HDR_PRESETS',
    'ARCHIVAL_PRESETS',

    # Preset functions
    'get_prores_config',
    'get_dnxhr_config',
    'get_prores_profile_for_bitrate',
    'get_dnxhr_profile_for_resolution',
    'estimate_prores_bitrate',
    'is_alpha_required',
    'get_all_presets',
    'get_recommended_preset',

    # Image sequence types (from sequence_exporter.py)
    'ImageFormat',
    'ColorPolicy',
    'BitDepthMode',
    'SequenceExportConfig',
    'ImageSequenceExporter',

    # Image sequence functions
    'create_sequence_exporter',

    # P8.1: Export Frame Provider
    'ExportFrameProvider',
    'ExportRange',
    'create_export_provider',
    'estimate_export_duration',

    # P8.2: PyAV Video Encoder
    'FFmpegEncoderConfig',
    'ColorMetadataConfig',
    'FFmpegVideoEncoder',
    'GifVideoEncoder',

    # Topos codec (from topos_encoder.py / topos_profiles.py)
    'ToposVideoEncoder',
    'TOPOS_PROFILE_TIERS',
    'AVAILABLE_TOPOS_TIERS',
    'ToposProfileTier',
    'available_topos_tiers',
    'default_topos_tier_id',
    'get_topos_tier',
    'get_topos_tier_or_none',
    'topos_capability_summary',
    'CodecType',
    'PIXEL_FORMAT_MAP',
    'ENCODER_NAME_MAP',
    'create_encoder_config',
    'get_recommended_codec',
    'SegmentRenderKey',
    'RenderCacheStats',
    'DiskRenderCache',
]
