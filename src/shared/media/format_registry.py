"""
Professional Media Format Registry.

影视后期文件类型注册表 — 统一管理视频、图像、音频文件扩展名的识别。
扩展名命中只表示类型分类，不证明当前解码后端可打开；例如 BRAW/R3D
需要专用 SDK，当前 TOPOS 媒体工厂尚未接入。所有模块应引用此处的常量，
避免各自维护导致不一致。

覆盖范围：
  - 专业编码：ProRes (all variants), DNxHR/DNxHD, XAVC, AVC-Intra, CineForm
  - 专业容器：MXF (OPAtom/OP1a), MOV, MP4, MKV
  - RAW 格式：CinemaDNG, ARRIRAW, RED R3D, BRAW, Phantom Cine
  - 序列帧：DPX, OpenEXR, TIFF, JPEG 2000, PNG
  - HDR/高精度：10/12/16-bit pixel formats, XYZ color space
"""

from __future__ import annotations

# ================================================================
# Video File Extensions
# ================================================================

VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    # --- 通用容器 ---
    '.mp4', '.mov', '.avi', '.mkv', '.m4v',
    '.flv', '.wmv', '.webm', '.ogv',
    '.ts', '.mts', '.m2ts',   # MPEG-TS (AVCHD, broadcast)
    '.vob', '.3gp', '.mpg', '.mpeg',

    # --- 专业广播/后期容器 ---
    '.mxf',                    # MXF (OPAtom/OP1a/IMF)
    '.prores',                 # ProRes standalone
    '.dnxhd', '.dnxhr',       # Avid DNxHD/DNxHR

    # --- Sony 专业 ---
    # XAVC 使用 .mxf 或 .mp4 容器，已在上面覆盖

    # --- RAW 摄影机格式 ---
    '.r3d',                    # RED R3D
    '.braw',                   # Blackmagic RAW
    '.ari',                    # ARRIRAW
    '.cine',                   # Phantom Cine (high-speed)
    '.dng',                    # CinemaDNG (also in IMAGE_EXTENSIONS for stills)

    # --- 序列帧 (单文件模式) ---
    # DPX/EXR 等通常以序列形式存在，单独文件也可能是视频
})

# ================================================================
# Image / Sequence Frame Extensions
# ================================================================

IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    # --- 通用图像 ---
    '.jpg', '.jpeg',
    '.png',
    '.bmp',
    '.gif',
    '.webp',

    # --- 专业序列帧 ---
    '.dpx',                    # DPX (SMPTE 268M, 数字中间片标准)
    '.exr',                    # OpenEXR (ILM, HDR/VFX 标准)
    '.tif', '.tiff',           # TIFF (16-bit, print/film)
    '.hdr',                    # Radiance HDR

    # --- JPEG 2000 (DCI/IMF) ---
    '.j2k',                    # JPEG 2000 codestream
    '.jp2',                    # JPEG 2000 Part 1
    '.j2c',                    # JPEG 2000 codestream (broadcast)

    # --- RAW 照片 (常见相机) ---
    '.cr2', '.cr3',            # Canon RAW
    '.nef',                    # Nikon RAW
    '.arw',                    # Sony RAW
    '.orf',                    # Olympus RAW
    '.raf',                    # Fujifilm RAW
    '.rw2',                    # Panasonic RAW
    '.pef',                    # Pentax RAW
    '.srw',                    # Samsung RAW
    '.nrw',                    # Nikon RAW (NEF variant)
    '.dng',                    # Adobe DNG / CinemaDNG

    # --- 其他 ---
    '.psd',                    # Adobe Photoshop
    '.heic', '.heif',          # HEIF/HEIC (Apple)
    '.tga',                    # Targa (VFX legacy)

    # --- Topos Image（TPIC 静态图，Topos Image 阶段 5）---
    '.toos',                   # Topos Image（TPIM envelope；内容以 TPIM magic
                               #  为最终依据见 is_toos_image；`.topos` 永久保留
                               #  工程包，不是图片别名——ADR-I001 D1）
})

# ================================================================
# Audio Extensions
# ================================================================

AUDIO_EXTENSIONS: frozenset[str] = frozenset({
    '.wav',                    # Waveform (PCM, broadcast standard)
    '.mp3',                    # MP3
    '.aac',                    # AAC
    '.flac',                   # FLAC (lossless)
    '.ogg',                    # Ogg Vorbis
    '.m4a',                    # MPEG-4 Audio
    '.aiff', '.aif',           # AIFF (Pro Tools standard)
    '.w64',                    # Wave64 (large files)
    '.bwf',                    # Broadcast Wave Format
    '.ac3',                    # Dolby Digital
    '.opus',                   # Opus
})

# ================================================================
# Subtitle / Caption Extensions
# ================================================================

SUBTITLE_EXTENSIONS: frozenset[str] = frozenset({
    '.srt',                     # SubRip
    '.vtt',                     # WebVTT
    '.ass', '.ssa',             # Advanced/SubStation Alpha
    '.ttml', '.dfxp', '.xml',    # TTML / DFXP timed text
})

# ================================================================
# 3D Model Extensions (阶段五：glTF/FBX/OBJ 导入)
# ================================================================

MODEL_3D_EXTENSIONS: frozenset[str] = frozenset({
    '.gltf',                   # glTF JSON (含外部 .bin + 贴图)
    '.glb',                    # glTF Binary (单文件)
    '.obj',                    # Wavefront OBJ
    '.stl',                    # Stereolithography (无 UV/法线，需生成)
    '.ply',                    # Polygon File Format
    '.fbx',                    # Autodesk FBX (需 pyassimp + libassimp)
    '.dae',                    # COLLADA
    '.3ds',                    # 3D Studio (需 pyassimp)
    '.blend',                  # Blender (需 pyassimp)
})

# ================================================================
# All Media Extensions (union)
# ================================================================

ALL_MEDIA_EXTENSIONS: frozenset[str] = (
    VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS
    | SUBTITLE_EXTENSIONS | MODEL_3D_EXTENSIONS
)


def is_video(path: str) -> bool:
    """Check if a file path has a video extension."""
    ext = _get_ext(path)
    return ext in VIDEO_EXTENSIONS

def is_model(path: str) -> bool:
    """Check if a file path has a 3D model extension."""
    ext = _get_ext(path)
    return ext in MODEL_3D_EXTENSIONS


def is_image(path: str) -> bool:
    """Check if a file path has an image extension."""
    ext = _get_ext(path)
    return ext in IMAGE_EXTENSIONS


def is_audio(path: str) -> bool:
    """Check if a file path has an audio extension."""
    ext = _get_ext(path)
    return ext in AUDIO_EXTENSIONS


def is_subtitle(path: str) -> bool:
    """Check if a file path has a supported subtitle extension."""
    ext = _get_ext(path)
    return ext in SUBTITLE_EXTENSIONS


def is_media(path: str) -> bool:
    """Check if a file path has any supported media extension."""
    ext = _get_ext(path)
    return ext in ALL_MEDIA_EXTENSIONS


def get_media_type(path: str) -> str:
    """
    Get the media type for a file path.

    Returns:
        'video', 'image', 'audio', 'model', or 'unknown'
    """
    ext = _get_ext(path)
    if ext in VIDEO_EXTENSIONS:
        return 'video'
    if ext in IMAGE_EXTENSIONS:
        return 'image'
    if ext in AUDIO_EXTENSIONS:
        return 'audio'
    if ext in SUBTITLE_EXTENSIONS:
        return 'subtitle'
    if ext in MODEL_3D_EXTENSIONS:
        return 'model'
    return 'unknown'


def is_toos_image(path: str, sniff_bytes: int = 64) -> bool:
    """按内容（TPIM magic）判定 Topos Image 静态图（spec §13：扩展名仅快速
    候选，magic 为最终依据）。`.toos` 扩展名缺失但内容为 TPIM 的文件同样
    命中；裸 TPIC packet（'TPIC' 开头）不是 .toos 图片，返回 False。
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(sniff_bytes)
    except OSError:
        return False
    return head[:4] == b'TPIM'


def _get_ext(path: str) -> str:
    """Extract lowercase extension from path."""
    import os
    return os.path.splitext(path)[1].lower()
