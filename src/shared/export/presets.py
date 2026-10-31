"""
Video Encoder Presets Module

Pre-configured encoder settings for professional video formats.
Provides ProRes and DNxHR presets optimized for NLE workflows.

## ProRes Profiles (Apple)

- PROXY: Lowest quality, smallest files - offline editing
- LT: Low quality, small files - rough cuts
- 422: Standard quality - general editing
- HQ: High quality - final cuts, broadcast
- 4444: Highest quality + alpha - VFX, compositing
- 4444 XQ: Maximum quality + alpha - archiving, HDR mastering

## DNxHR Profiles (Avid)

- LB: Low Bandwidth - offline editing
- SQ: Standard Quality - general editing
- HQ: High Quality - final cuts
- HQX: High Quality Extended (10-bit) - broadcast
- 444: Highest quality + alpha - VFX, compositing

## Bitrate Reference

ProRes bitrates are approximate at 1080p30:
- Proxy: 45 Mbps
- LT: 102 Mbps
- 422: 147 Mbps
- HQ: 220 Mbps
- 4444: 330 Mbps
- 4444 XQ: 500 Mbps

DNxHR bitrates vary with resolution.

## Container Compatibility

- ProRes: MOV (QuickTime)
- DNxHR: MXF (Avid) or MOV (QuickTime)

## NLE Support

Both ProRes and DNxHR are universally supported:
- DaVinci Resolve
- Adobe Premiere Pro
- Final Cut Pro
- Avid Media Composer
- After Effects
- Nuke
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Optional

from .encoder import (
    VideoEncoderConfig,
    ColorSpace,
    ColorTransfer,
    ColorPrimaries,
)


class ProResProfile(Enum):
    """
    Apple ProRes codec profiles.

    Quality ranking (lowest to highest):
    PROXY < LT < STANDARD < HQ < RGBA < RGBA_XQ

    Each profile has specific target bitrate and pixel format.

    Note: Enum member names cannot start with digits in Python.
    - 422 -> STANDARD
    - 4444 -> RGBA
    - 4444 XQ -> RGBA_XQ
    """
    PROXY = 'proxy'      # ProRes Proxy - Offline editing
    LT = 'lt'            # ProRes LT - Rough cuts
    STANDARD = '422'     # ProRes 422 - General editing
    HQ = 'hq'            # ProRes 422 HQ - Final cuts, broadcast
    RGBA = '4444'        # ProRes 4444 - VFX, compositing (with alpha)
    RGBA_XQ = '4444xq'   # ProRes 4444 XQ - Maximum quality (12-bit)


class DNxHRProfile(Enum):
    """
    Avid DNxHR codec profiles.

    DNxHR is designed for HD and UHD resolutions.
    Quality ranking (lowest to highest):
    LB < SQ < HQ < HQX < RGBA

    Note: Enum member names cannot start with digits in Python.
    - 444 -> RGBA
    """
    LB = 'lb'            # Low Bandwidth - Offline editing (8-bit)
    SQ = 'sq'            # Standard Quality - General editing (8-bit)
    HQ = 'hq'            # High Quality - Final cuts (8-bit)
    HQX = 'hqx'          # High Quality Extended - Broadcast (10-bit)
    RGBA = '444'         # DNxHR 444 - VFX, compositing (10-bit + alpha)


class H264Profile(Enum):
    """H.264/AVC codec profiles."""
    BASELINE = 'baseline'
    MAIN = 'main'
    HIGH = 'high'
    HIGH10 = 'high10'


class H265Profile(Enum):
    """H.265/HEVC codec profiles."""
    MAIN = 'main'
    MAIN10 = 'main10'
    MAIN444_8 = 'main444_8'
    MAIN444_10 = 'main444_10'


# =============================================================================
# ProRes Presets
# =============================================================================

PRORES_PRESETS: Dict[ProResProfile, VideoEncoderConfig] = {
    ProResProfile.PROXY: VideoEncoderConfig(
        codec='prores',
        profile='proxy',
        pix_fmt='yuv422p',
        bit_depth=8,
        has_alpha=False,
        target_bitrate=45,  # Mbps @ 1080p30
        container='mov',
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
        extra_options={
            'vendor': 'apl0',  # Apple vendor code
            'bits_per_mb': '0',  # Auto
        }
    ),

    ProResProfile.LT: VideoEncoderConfig(
        codec='prores',
        profile='lt',
        pix_fmt='yuv422p',
        bit_depth=8,
        has_alpha=False,
        target_bitrate=102,  # Mbps @ 1080p30
        container='mov',
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
        extra_options={
            'vendor': 'apl0',
            'bits_per_mb': '0',
        }
    ),

    ProResProfile.STANDARD: VideoEncoderConfig(
        codec='prores',
        profile='422',  # Standard ProRes 422
        pix_fmt='yuv422p10le',
        bit_depth=10,
        has_alpha=False,
        target_bitrate=147,  # Mbps @ 1080p30
        container='mov',
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
        extra_options={
            'vendor': 'apl0',
            'bits_per_mb': '0',
        }
    ),

    ProResProfile.HQ: VideoEncoderConfig(
        codec='prores',
        profile='hq',
        pix_fmt='yuv422p10le',
        bit_depth=10,
        has_alpha=False,
        target_bitrate=220,  # Mbps @ 1080p30
        container='mov',
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
        extra_options={
            'vendor': 'apl0',
            'bits_per_mb': '0',
        }
    ),

    ProResProfile.RGBA: VideoEncoderConfig(
        codec='prores',
        profile='4444',
        pix_fmt='yuva444p10le',
        bit_depth=10,
        has_alpha=True,
        target_bitrate=330,  # Mbps @ 1080p30 (with alpha)
        container='mov',
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
        extra_options={
            'vendor': 'apl0',
            'bits_per_mb': '0',
        }
    ),

    ProResProfile.RGBA_XQ: VideoEncoderConfig(
        codec='prores',
        profile='4444xq',
        pix_fmt='yuva444p12le',
        bit_depth=12,
        has_alpha=True,
        target_bitrate=500,  # Mbps @ 1080p30 (12-bit + alpha)
        container='mov',
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
        extra_options={
            'vendor': 'apl0',
            'bits_per_mb': '0',
        }
    ),
}


# =============================================================================
# DNxHR Presets
# =============================================================================

DNXHR_PRESETS: Dict[DNxHRProfile, VideoEncoderConfig] = {
    DNxHRProfile.LB: VideoEncoderConfig(
        codec='dnxhr',
        profile='lb',
        pix_fmt='yuv422p',
        bit_depth=8,
        has_alpha=False,
        target_bitrate=None,  # Variable based on resolution
        container='mxf',  # Avid standard
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
        extra_options={
            # DNxHR uses profile string directly
        }
    ),

    DNxHRProfile.SQ: VideoEncoderConfig(
        codec='dnxhr',
        profile='sq',
        pix_fmt='yuv422p',
        bit_depth=8,
        has_alpha=False,
        target_bitrate=None,  # Variable
        container='mxf',
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
    ),

    DNxHRProfile.HQ: VideoEncoderConfig(
        codec='dnxhr',
        profile='hq',
        pix_fmt='yuv422p',
        bit_depth=8,
        has_alpha=False,
        target_bitrate=None,  # Variable
        container='mxf',
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
    ),

    DNxHRProfile.HQX: VideoEncoderConfig(
        codec='dnxhr',
        profile='hqx',
        pix_fmt='yuv422p10le',
        bit_depth=10,
        has_alpha=False,
        target_bitrate=None,  # Variable
        container='mxf',
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
    ),

    DNxHRProfile.RGBA: VideoEncoderConfig(
        codec='dnxhr',
        profile='444',
        pix_fmt='yuva444p10le',
        bit_depth=10,
        has_alpha=True,
        target_bitrate=None,  # Variable
        container='mxf',
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
    ),
}


# =============================================================================
# H.264/H.265 Broadcast Presets
# =============================================================================

H264_BROADCAST_PRESETS: Dict[str, VideoEncoderConfig] = {
    'h264_broadcast_sd': VideoEncoderConfig(
        codec='h264',
        profile='high',
        pix_fmt='yuv420p',
        bit_depth=8,
        has_alpha=False,
        target_bitrate=8,
        max_bitrate=10,
        container='mp4',
        preset='slow',
        quality=18,  # CRF fallback
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
    ),

    'h264_broadcast_hd': VideoEncoderConfig(
        codec='h264',
        profile='high',
        pix_fmt='yuv420p',
        bit_depth=8,
        has_alpha=False,
        target_bitrate=20,
        max_bitrate=25,
        container='mp4',
        preset='slow',
        quality=18,
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
    ),

    'h264_web': VideoEncoderConfig(
        codec='h264',
        profile='high',
        pix_fmt='yuv420p',
        bit_depth=8,
        has_alpha=False,
        target_bitrate=None,
        container='mp4',
        preset='medium',
        quality=23,  # CRF for variable bitrate
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
    ),
}

H265_HDR_PRESETS: Dict[str, VideoEncoderConfig] = {
    'hevc_hdr10': VideoEncoderConfig(
        codec='hevc',
        profile='main10',
        pix_fmt='yuv420p10le',
        bit_depth=10,
        has_alpha=False,
        target_bitrate=None,
        container='mp4',
        preset='slow',
        quality=20,
        color_space=ColorSpace.BT2020_NCL,
        color_transfer=ColorTransfer.SMPTE2084,
        color_primaries=ColorPrimaries.BT2020,
        extra_options={
            'x265-params': 'hdr10=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc',
        }
    ),

    'hevc_hlg': VideoEncoderConfig(
        codec='hevc',
        profile='main10',
        pix_fmt='yuv420p10le',
        bit_depth=10,
        has_alpha=False,
        target_bitrate=None,
        container='mp4',
        preset='slow',
        quality=20,
        color_space=ColorSpace.BT2020_NCL,
        color_transfer=ColorTransfer.HLG,
        color_primaries=ColorPrimaries.BT2020,
        extra_options={
            'x265-params': 'hlg=1:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc',
        }
    ),
}


# =============================================================================
# Archival Presets
# =============================================================================

ARCHIVAL_PRESETS: Dict[str, VideoEncoderConfig] = {
    'ffv1_archive': VideoEncoderConfig(
        codec='ffv1',
        profile=None,
        pix_fmt='yuva444p16le',  # Maximum quality
        bit_depth=16,
        has_alpha=True,
        target_bitrate=None,
        container='mkv',  # MKV recommended for FFV1
        color_space=ColorSpace.UNSPECIFIED,
        color_transfer=ColorTransfer.UNSPECIFIED,
        color_primaries=ColorPrimaries.UNSPECIFIED,
        extra_options={
            'coder': '1',     # Range coder
            'context': '1',   # Context-adaptive
            'level': '3',     # FFV1 version 3
        }
    ),

    'prores_archive': VideoEncoderConfig(
        codec='prores',
        profile='4444xq',
        pix_fmt='yuva444p12le',
        bit_depth=12,
        has_alpha=True,
        target_bitrate=500,
        container='mov',
        color_space=ColorSpace.BT709,
        color_transfer=ColorTransfer.BT709,
        color_primaries=ColorPrimaries.BT709,
    ),
}


# =============================================================================
# Helper Functions
# =============================================================================

def get_prores_config(profile: ProResProfile) -> VideoEncoderConfig:
    """
    Get ProRes encoder configuration for specified profile.

    Args:
        profile: ProRes profile enum value

    Returns:
        VideoEncoderConfig instance

    Raises:
        KeyError: If profile not found
    """
    return PRORES_PRESETS[profile]


def get_dnxhr_config(profile: DNxHRProfile) -> VideoEncoderConfig:
    """
    Get DNxHR encoder configuration for specified profile.

    Args:
        profile: DNxHR profile enum value

    Returns:
        VideoEncoderConfig instance

    Raises:
        KeyError: If profile not found
    """
    return DNXHR_PRESETS[profile]


def get_prores_profile_for_bitrate(bitrate_mbps: int) -> Optional[ProResProfile]:
    """
    Recommend ProRes profile based on target bitrate.

    Args:
        bitrate_mbps: Target bitrate in Mbps

    Returns:
        Recommended ProResProfile or None if no match
    """
    # Approximate bitrate thresholds at 1080p30
    if bitrate_mbps <= 50:
        return ProResProfile.PROXY
    elif bitrate_mbps <= 110:
        return ProResProfile.LT
    elif bitrate_mbps <= 160:
        return ProResProfile.STANDARD
    elif bitrate_mbps <= 250:
        return ProResProfile.HQ
    elif bitrate_mbps <= 400:
        return ProResProfile.RGBA
    else:
        return ProResProfile.RGBA_XQ


def get_dnxhr_profile_for_resolution(
    width: int,
    height: int,
    quality_level: str = 'hq'
) -> DNxHRProfile:
    """
    Recommend DNxHR profile based on resolution and quality preference.

    ``quality_level`` selects the requested tier; the resolution can only
    *raise* the recommendation, never lower it (AUD-33212: width/height were
    accepted but previously ignored).  At UHD and above the 10-bit HQX tier is
    the Avid mastering recommendation, so an LB/SQ/HQ request is upgraded to
    HQX there; an explicit HQX/444 request is kept as-is.

    Args:
        width: Video width
        height: Video height
        quality_level: 'lb', 'sq', 'hq', 'hqx', '444'

    Returns:
        Recommended DNxHRProfile
    """
    # Map quality level to profile
    profile_map = {
        'lb': DNxHRProfile.LB,
        'sq': DNxHRProfile.SQ,
        'hq': DNxHRProfile.HQ,
        'hqx': DNxHRProfile.HQX,
        '444': DNxHRProfile.RGBA,
    }
    profile = profile_map.get(quality_level, DNxHRProfile.HQ)
    if max(width, height) >= 3840 and profile in (
            DNxHRProfile.LB, DNxHRProfile.SQ, DNxHRProfile.HQ):
        return DNxHRProfile.HQX
    return profile


def estimate_prores_bitrate(
    profile: ProResProfile,
    width: int,
    height: int,
    fps: float
) -> int:
    """
    Estimate ProRes bitrate for given resolution and framerate.

    ProRes bitrate scales with resolution and framerate.
    Base bitrate is at 1080p30.

    Args:
        profile: ProRes profile
        width: Video width
        height: Video height
        fps: Framerate

    Returns:
        Estimated bitrate in Mbps
    """
    # Get base bitrate at 1080p30
    base_bitrate = PRORES_PRESETS[profile].target_bitrate or 100

    # Resolution factor (relative to 1080p)
    pixels_1080p = 1920 * 1080
    pixels_current = width * height
    resolution_factor = pixels_current / pixels_1080p

    # Framerate factor (relative to 30fps)
    fps_factor = fps / 30.0

    # Estimated bitrate
    estimated = base_bitrate * resolution_factor * fps_factor

    # Round to nearest Mbps
    return int(estimated)


def is_alpha_required(profile: ProResProfile | DNxHRProfile) -> bool:
    """
    Check if profile requires/supports alpha channel.

    Args:
        profile: Codec profile

    Returns:
        True if alpha is supported
    """
    if isinstance(profile, ProResProfile):
        return profile in (ProResProfile.RGBA, ProResProfile.RGBA_XQ)
    elif isinstance(profile, DNxHRProfile):
        return profile == DNxHRProfile.RGBA
    return False


def get_all_presets() -> Dict[str, Dict[str, VideoEncoderConfig]]:
    """
    Get all preset configurations grouped by codec family.

    Returns:
        Dictionary with preset groups
    """
    return {
        'prores': {p.name: config for p, config in PRORES_PRESETS.items()},
        'dnxhr': {p.name: config for p, config in DNXHR_PRESETS.items()},
        'h264': H264_BROADCAST_PRESETS,
        'hevc': H265_HDR_PRESETS,
        'archive': ARCHIVAL_PRESETS,
    }


def get_recommended_preset(
    use_case: str,
    has_alpha: bool = False,
    is_hdr: bool = False
) -> VideoEncoderConfig:
    """
    Get recommended preset for specific use case.

    Args:
        use_case: 'offline', 'editing', 'broadcast', 'vfx', 'web', 'archive'
        has_alpha: Whether alpha channel is needed
        is_hdr: Whether HDR output is needed

    Returns:
        Recommended VideoEncoderConfig
    """
    # Offline editing (low quality, small files)
    if use_case == 'offline':
        if has_alpha:
            return PRORES_PRESETS[ProResProfile.RGBA]
        return PRORES_PRESETS[ProResProfile.PROXY]

    # General editing
    if use_case == 'editing':
        if has_alpha:
            return PRORES_PRESETS[ProResProfile.RGBA]
        return PRORES_PRESETS[ProResProfile.STANDARD]

    # Broadcast delivery
    if use_case == 'broadcast':
        if is_hdr:
            return H265_HDR_PRESETS['hevc_hdr10']
        return H264_BROADCAST_PRESETS['h264_broadcast_hd']

    # VFX/compositing
    if use_case == 'vfx':
        if is_hdr:
            return PRORES_PRESETS[ProResProfile.RGBA_XQ]
        return PRORES_PRESETS[ProResProfile.RGBA]

    # Web delivery
    if use_case == 'web':
        return H264_BROADCAST_PRESETS['h264_web']

    # Archival
    if use_case == 'archive':
        return ARCHIVAL_PRESETS['ffv1_archive']

    # Default: ProRes 422
    return PRORES_PRESETS[ProResProfile.STANDARD]