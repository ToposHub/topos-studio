"""指纹算法的统一接口（Protocol）+ 分派器。

所有指纹实现遵循同一契约：``compute(path) -> str`` / ``verify(path, fp) -> bool``。
指纹字符串带**类型前缀**（如 ``xxh3_3pt:abc123``），让 relink 模块知道用哪种算法验证。

前缀约定：
    xxh3_3pt:  视频采样哈希（3 点采样，精度高）
    chroma:    Chromaprint 声纹（音频，精度高）
    phash:     感知哈希（图片，精度中）
    size:      纯文件大小兜底（精度低，任何类型在依赖缺失时降级到此）
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class Fingerprinter(Protocol):
    """指纹算法契约。所有实现必须满足此接口（duck typing）。"""

    def compute(self, path: str) -> str:
        """计算文件的内容指纹。

        Returns:
            带类型前缀的指纹字符串，如 ``"xxh3_3pt:a1b2c3d4"``。
            依赖缺失时降级为 ``"size:<bytes>"``（精度低但不崩溃）。
        """
        ...

    def verify(self, path: str, fingerprint: str) -> bool:
        """验证某文件是否匹配给定指纹。

        用于 relink：在候选目录里对每个文件调 verify，命中即认出原素材。
        实现应比 compute 快（如 pHash 的 verify 可先比 file_size 再算 hash）。
        """
        ...


# ---------------------------------------------------------------------- #
# 前缀定义（与各实现的 fingerprint 格式对应）
# ---------------------------------------------------------------------- #

PREFIX_VIDEO_SAMPLE = "xxh3_3pt"   # 视频采样哈希
PREFIX_AUDIO_CHROMA = "chroma"     # Chromaprint 声纹
PREFIX_IMAGE_PHASH = "phash"       # 感知哈希
PREFIX_SIZE_ONLY = "size"          # 纯大小兜底（最低精度）


def parse_fingerprint_type(fingerprint: str) -> str:
    """从指纹字符串解析出类型前缀。

    "xxh3_3pt:abc123" → "xxh3_3pt"
    "chroma:xyz"      → "chroma"
    无前缀的视为 "size"（兼容历史数据）
    """
    if ":" in fingerprint:
        return fingerprint.split(":", 1)[0]
    return PREFIX_SIZE_ONLY


def _size_only_fingerprint(path: str) -> str:
    """退化路径：纯文件大小兜底（精度最低，但跨平台无依赖）。

    relink 时只能靠 Level 1（同名同大小）匹配，无法识别改名/换盘。
    """
    return f"{PREFIX_SIZE_ONLY}:{os.path.getsize(path)}"


def _size_only_verify(path: str, fingerprint: str) -> bool:
    """退化验证：只比文件大小。"""
    try:
        return f"{PREFIX_SIZE_ONLY}:{os.path.getsize(path)}" == fingerprint
    except OSError:
        return False


# 注：分派器 compute_fingerprint / verify_fingerprint 定义在 dispatch.py，
# 避免循环导入（具体实现 video/audio/image 反向 import base 的常量）。
# 调用方从 fingerprint 包根导入即可：from apps.topos_vault.fingerprint import compute_fingerprint
