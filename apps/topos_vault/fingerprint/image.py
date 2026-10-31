"""图片指纹：pHash 感知哈希。

为什么不能用字节哈希：图片重新保存（JPEG 质量、PNG 压缩级别）、轻微缩放/裁剪，
字节完全变了，但人眼看起来是同一张。pHash 基于 DCT（离散余弦变换）的低频分量，
对压缩/缩放/轻微裁剪鲁棒。

依赖 ``imagehash`` + ``Pillow``。未安装时降级到 size:。
"""

from __future__ import annotations


from apps.topos_vault.fingerprint.base import (
    PREFIX_IMAGE_PHASH,
    _size_only_fingerprint,
    _size_only_verify,
)

try:
    import imagehash  # type: ignore
    from PIL import Image  # type: ignore
    _HAS_IMAGEHASH = True
except ImportError:
    _HAS_IMAGEHASH = False


# pHash 哈希位数：64-bit（8x8 DCT）——足够区分，存储紧凑
_PHASH_SIZE = 8  # hash_size 参数：8 → 64-bit hash


class PerceptualHasher:
    """pHash 感知哈希指纹器。"""

    def compute(self, path: str) -> str:
        """计算图片感知哈希。"""
        if not _HAS_IMAGEHASH:
            return _size_only_fingerprint(path)

        try:
            with Image.open(path) as img:
                # pHash：缩放到 32x32 + DCT，取左上 8x8 的低频系数
                # 对缩放/压缩/轻微裁剪鲁棒
                h = imagehash.phash(img, hash_size=_PHASH_SIZE)
                return f"{PREFIX_IMAGE_PHASH}:{str(h)}"
        except Exception:
            # 图片损坏或格式不支持
            return _size_only_fingerprint(path)

    def verify(self, path: str, fingerprint: str) -> bool:
        """验证图片是否匹配感知哈希。

        pHash 的 verify 允许**汉明距离 ≤ 阈值**（而非完全相等）——
        这正是 pHash 的优势：轻微压缩/重保存后哈希可能差几位，但仍认为是同一张。

        本方法用阈值 5（64-bit 中 5 位以内差异 ≈ 视觉上几乎相同）。
        """
        if not fingerprint.startswith(PREFIX_IMAGE_PHASH + ":"):
            return _size_only_verify(path, fingerprint)

        if not _HAS_IMAGEHASH:
            return _size_only_verify(path, fingerprint)

        # 解析存储的哈希
        stored_str = fingerprint.split(":", 1)[1]
        try:
            stored_hash = imagehash.hex_to_hash(stored_str)
        except (ValueError, Exception):
            return False

        try:
            with Image.open(path) as img:
                current_hash = imagehash.phash(img, hash_size=_PHASH_SIZE)
        except Exception:
            return False

        # 汉明距离：不同 bit 数。≤ 阈值认为匹配
        hamming = current_hash - stored_hash
        return hamming <= _PHASH_HAMMING_THRESHOLD


# 汉明距离阈值：64-bit pHash 中，≤5 认为是同一张图
# （经验值：JPEG 重保存通常引入 0-3 位差异，轻微裁剪 3-8 位）
_PHASH_HAMMING_THRESHOLD = 5


def is_phash_available() -> bool:
    """pHash 是否可用。"""
    return _HAS_IMAGEHASH
