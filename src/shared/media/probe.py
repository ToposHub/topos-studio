"""
FFmpeg/PyAV 环境检测模块。

提供 PyAV/FFmpeg 可用性检测和版本信息查询功能。

## 职责

- 检测 PyAV/FFmpeg 是否可用
- 获取 FFmpeg 版本信息
- 查询支持的编解码器

## 设计原则

- 不抛出异常：缺依赖时返回明确状态
- 延迟导入：首次调用时才尝试导入 PyAV
- 缓存结果：避免重复检测
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 缓存检测结果
_cached_ffmpeg_available: Optional[bool] = None
_cached_ffmpeg_version: Optional[str] = None
_cached_supported_codecs: Optional[list[str]] = None


def check_ffmpeg_available() -> bool:
    """
    检查 PyAV/FFmpeg 是否可用。

    尝试导入 PyAV 并验证 FFmpeg 库是否正确加载。
    结果会被缓存以避免重复检测。

    Returns:
        可用返回 True，否则返回 False
    """
    global _cached_ffmpeg_available

    if _cached_ffmpeg_available is not None:
        return _cached_ffmpeg_available

    try:
        import av
        # 尝试访问 FFmpeg 版本以验证库加载成功
        # PyAV 12+ 使用 ffmpeg_version_info，旧版使用 av.lib.av_version_info()
        if hasattr(av, 'ffmpeg_version_info'):
            _ = av.ffmpeg_version_info
        elif hasattr(av, 'lib') and hasattr(av.lib, 'av_version_info'):
            _ = av.lib.av_version_info()
        else:
            # 尝试打开一个简单的容器验证
            _ = av.codecs_available

        _cached_ffmpeg_available = True
        logger.debug("PyAV/FFmpeg is available")
        return True
    except ImportError:
        logger.warning("PyAV not installed")
        _cached_ffmpeg_available = False
        return False
    except Exception as e:
        logger.warning(f"PyAV/FFmpeg initialization failed: {e}")
        _cached_ffmpeg_available = False
        return False


def get_ffmpeg_version() -> Optional[str]:
    """
    获取 FFmpeg 版本信息。

    Returns:
        版本字符串（如 "6.0" 或 "8.0.1"），不可用时返回 None
    """
    global _cached_ffmpeg_version

    if _cached_ffmpeg_version is not None:
        return _cached_ffmpeg_version

    if not check_ffmpeg_available():
        return None

    try:
        import av
        # PyAV 12+ 使用 ffmpeg_version_info（字符串）
        if hasattr(av, 'ffmpeg_version_info'):
            version_info = av.ffmpeg_version_info
            if isinstance(version_info, str):
                _cached_ffmpeg_version = version_info
                return _cached_ffmpeg_version

        # 旧版使用 av.lib.av_version_info()（元组）
        if hasattr(av, 'lib') and hasattr(av.lib, 'av_version_info'):
            version_info = av.lib.av_version_info()
            if isinstance(version_info, tuple):
                # 过滤掉末尾的 0，如 (6, 0, 0) -> "6.0"
                parts = list(version_info)
                while len(parts) > 1 and parts[-1] == 0:
                    parts.pop()
                _cached_ffmpeg_version = ".".join(str(p) for p in parts)
                return _cached_ffmpeg_version

        # 从 library_versions 获取 libavcodec 版本
        if hasattr(av, 'library_versions') and av.library_versions:
            libavcodec = av.library_versions.get('libavcodec')
            if libavcodec and isinstance(libavcodec, tuple):
                parts = list(libavcodec)
                while len(parts) > 1 and parts[-1] == 0:
                    parts.pop()
                _cached_ffmpeg_version = ".".join(str(p) for p in parts)
                return _cached_ffmpeg_version

        return None
    except Exception as e:
        logger.warning(f"Failed to get FFmpeg version: {e}")
        return None


def get_supported_codecs() -> list[str]:
    """
    获取支持的编解码器列表。

    返回当前 FFmpeg 支持的所有编解码器名称。
    结果会被缓存以避免重复查询。

    Returns:
        编解码器名称列表（如 ['h264', 'hevc', 'prores', ...]）
    """
    global _cached_supported_codecs

    if _cached_supported_codecs is not None:
        return _cached_supported_codecs

    if not check_ffmpeg_available():
        _cached_supported_codecs = []
        return []

    try:
        import av
        codecs: list[str] = []

        # PyAV 12+ 使用 codecs_available（set）
        if hasattr(av, 'codecs_available'):
            codec_set = av.codecs_available
            if isinstance(codec_set, set):
                codecs = list(codec_set)
            elif isinstance(codec_set, (list, tuple)):
                codecs = list(codec_set)

        # 旧版使用 av.codecs 迭代器
        if not codecs and hasattr(av, 'codecs'):
            for codec in av.codecs:
                if hasattr(codec, 'name'):
                    codecs.append(codec.name)
                elif hasattr(codec, 'id'):
                    codecs.append(str(codec.id))

        _cached_supported_codecs = sorted(set(codecs))
        logger.debug(f"Found {len(_cached_supported_codecs)} supported codecs")
        return _cached_supported_codecs
    except Exception as e:
        logger.warning(f"Failed to get supported codecs: {e}")
        _cached_supported_codecs = []
        return []


def check_codec_supported(codec_name: str) -> bool:
    """
    检查特定编解码器是否支持。

    Args:
        codec_name: 编解码器名称（如 'h264', 'hevc', 'prores'）

    Returns:
        支持返回 True，否则返回 False
    """
    if not check_ffmpeg_available():
        return False

    supported = get_supported_codecs()

    # 精确匹配
    if codec_name in supported:
        return True

    # 某些编解码器有别名
    codec_aliases = {
        'h264': ['libx264', 'h264'],
        'hevc': ['libx265', 'hevc', 'h265'],
        'prores': ['prores', 'prores_ks', 'prores_aw'],
        'dnxhr': ['dnxhr', 'dnxhd'],
        'dnxhd': ['dnxhr', 'dnxhd'],
        'vp9': ['libvpx-vp9', 'vp9'],
        'av1': ['libaom-av1', 'av1', 'libdav1d'],
    }

    if codec_name.lower() in codec_aliases:
        for alias in codec_aliases[codec_name.lower()]:
            if alias in supported:
                return True

    return False


def clear_cache() -> None:
    """
    清除检测缓存。

    用于重新检测环境（如安装/卸载 PyAV 后）。
    """
    global _cached_ffmpeg_available, _cached_ffmpeg_version, _cached_supported_codecs
    _cached_ffmpeg_available = None
    _cached_ffmpeg_version = None
    _cached_supported_codecs = None
    logger.debug("Environment cache cleared")