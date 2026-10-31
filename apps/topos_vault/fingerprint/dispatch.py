"""指纹分派器（独立模块，避免循环导入）。

base.py 定义常量和 Protocol；video/audio/image 各自实现并 import base 的常量；
本模块单向 import 所有实现做分派，打破环。

调用方从包根导入即可：
    from apps.topos_vault.fingerprint import compute_fingerprint, verify_fingerprint
"""

from __future__ import annotations

from apps.topos_vault.fingerprint.base import (
    PREFIX_AUDIO_CHROMA,
    PREFIX_IMAGE_PHASH,
    PREFIX_SIZE_ONLY,
    PREFIX_VIDEO_SAMPLE,
    _size_only_fingerprint,
    _size_only_verify,
    parse_fingerprint_type,
)
from apps.topos_vault.fingerprint.video import VideoSampleHasher
from apps.topos_vault.fingerprint.audio import ChromaprintFingerprinter
from apps.topos_vault.fingerprint.image import PerceptualHasher


def compute_fingerprint(path: str, media_type: str) -> str:
    """按素材类型计算指纹（分派到具体实现）。

    Args:
        path: 文件绝对路径（NFC 规范化由调用方负责）。
        media_type: video / audio / image / image_sequence

    Returns:
        带类型前缀的指纹字符串。依赖缺失时降级到 ``size:`` 兜底。
    """
    media_type = media_type.lower()

    if media_type == "video":
        return VideoSampleHasher().compute(path)

    if media_type == "audio":
        return ChromaprintFingerprinter().compute(path)

    if media_type in ("image", "image_sequence"):
        return PerceptualHasher().compute(path)

    # 未知类型：纯大小兜底
    return _size_only_fingerprint(path)


def verify_fingerprint(path: str, fingerprint: str) -> bool:
    """按指纹前缀选择验证算法。

    不依赖 media_type——指纹自带类型前缀，verify 据此分派。
    这样 relink 扫描候选目录时，不需要知道每个文件是什么类型。
    """
    fp_type = parse_fingerprint_type(fingerprint)

    if fp_type == PREFIX_VIDEO_SAMPLE:
        return VideoSampleHasher().verify(path, fingerprint)
    if fp_type == PREFIX_AUDIO_CHROMA:
        return ChromaprintFingerprinter().verify(path, fingerprint)
    if fp_type == PREFIX_IMAGE_PHASH:
        return PerceptualHasher().verify(path, fingerprint)
    if fp_type == PREFIX_SIZE_ONLY:
        return _size_only_verify(path, fingerprint)

    return False
