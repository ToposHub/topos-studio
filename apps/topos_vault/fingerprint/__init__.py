"""素材内容指纹（用于素材移动/重命名后的重连）。

三类素材用不同的指纹算法——这是核心设计：

    video  → xxhash 采样哈希（首+中+尾各 1MB + file_size）
              快（500ms/8GB 片），对视频足够可靠（视频帧高度非随机，采样不撞）
    audio  → Chromaprint 声纹
              重编码/变码率仍能认出同一首（字节哈希对音频无效）
    image  → pHash 感知哈希
              缩放/压缩/裁剪仍能认出同一张（字节哈希对图片无效）

每个实现都有**退化路径**：依赖库没装时，降级到 ``file_size`` 兜底（精度低但不崩溃），
并在 fingerprint 前缀标明精度（``xxh3_3pt:`` / ``chroma:`` / ``phash:`` / ``size:``），
让重连模块知道该用哪个 Level 匹配。
"""

from apps.topos_vault.fingerprint.base import (
    Fingerprinter,
    parse_fingerprint_type,
)
from apps.topos_vault.fingerprint.dispatch import (
    compute_fingerprint,
    verify_fingerprint,
)
from apps.topos_vault.fingerprint.video import VideoSampleHasher
from apps.topos_vault.fingerprint.audio import ChromaprintFingerprinter
from apps.topos_vault.fingerprint.image import PerceptualHasher

__all__ = [
    "Fingerprinter",
    "compute_fingerprint",
    "verify_fingerprint",
    "parse_fingerprint_type",
    "VideoSampleHasher",
    "ChromaprintFingerprinter",
    "PerceptualHasher",
]
