"""音频指纹：Chromaprint 声纹。

为什么不能用字节哈希：音乐换个码率（MP3 320k → 128k），字节完全变了，
但内容是同一首。Chromaprint 基于音频的频谱特征生成声纹，重编码/变比特率
仍能认出同一首曲子。

依赖 ``pyacoustid``（封装 libchromaprint）+ 系统 ffmpeg。未安装时降级到 size:。

跨平台注意：
- macOS: ``brew install chromaprint``（提供 libchromaprint）
- Linux: ``apt install libchromaprint-tools`` 或从源码编译
- Windows: 需单独下载 chromaprint-fpcalc.exe 并加入 PATH（文档说明）
"""

from __future__ import annotations

import shutil
import subprocess

from apps.topos_vault.fingerprint.base import (
    PREFIX_AUDIO_CHROMA,
    _size_only_fingerprint,
    _size_only_verify,
)

try:
    import acoustid  # type: ignore  # pyacoustid
    _HAS_ACOUSTID = True
except ImportError:
    _HAS_ACOUSTID = False


class ChromaprintFingerprinter:
    """Chromaprint 声纹指纹器。

    优先用 pyacoustid 的 Python 绑定（直接调 libchromaprint）；
    退化到 fpcalc 命令行工具（subprocess）；
    都没有则降级到 size:。
    """

    def compute(self, path: str) -> str:
        """计算音频声纹指纹。"""
        # 路径 1：pyacoustid Python 绑定
        if _HAS_ACOUSTID:
            try:
                duration, fp = acoustid.fingerprint_file(path)
                # fp 是字节列表（chromaprint 原始格式），编码成紧凑的 hex/base64
                # acoustid 的 fingerprint 是 list[int]，每个 int 32-bit
                fp_str = _encode_fingerprint(fp)
                return f"{PREFIX_AUDIO_CHROMA}:{fp_str[:64]}:{int(duration)}"
            except Exception:
                # pyacoustid 失败（libchromaprint 缺失/损坏），尝试 fpcalc
                pass

        # 路径 2：fpcalc 命令行
        result = _try_fpcalc(path)
        if result is not None:
            fp_str, duration = result
            return f"{PREFIX_AUDIO_CHROMA}:{fp_str[:64]}:{int(duration)}"

        # 路径 3：降级
        return _size_only_fingerprint(path)

    def verify(self, path: str, fingerprint: str) -> bool:
        """验证音频是否匹配声纹指纹。

        声纹比对是近似匹配（同一首歌不同编码的声纹不是完全相等，而是高相似度）。
        本方法用简化策略：计算新声纹，比较前缀相似度。
        精确的声纹比对（Hamming 距离）留到 relink 模块做更细致的实现。
        """
        if not fingerprint.startswith(PREFIX_AUDIO_CHROMA + ":"):
            # 可能是 size: 降级指纹
            return _size_only_verify(path, fingerprint)

        new_fp = self.compute(path)
        if not new_fp.startswith(PREFIX_AUDIO_CHROMA + ":"):
            return False

        # 简化比对：声纹前缀相等即认为匹配（粗筛）
        # 真正的 Hamming 距离比对在 relink Level 2 用更精细的实现
        return _chroma_prefix(new_fp) == _chroma_prefix(fingerprint)


def _encode_fingerprint(fp_bytes) -> str:
    """把 acoustid 的 fingerprint（list[int]）编码成紧凑字符串。"""
    # acoustid.fingerprint_file 返回的 fp 是 list[int]
    # 转成 hex 字符串做存储（前 64 字符足够区分）
    return "".join(f"{int(x) & 0xFFFFFFFF:08x}" for x in fp_bytes[:8])


def _chroma_prefix(fingerprint: str) -> str:
    """提取 chroma 指纹的声纹部分（去掉前缀和 duration）。"""
    # 格式：chroma:<hex>:<duration>
    parts = fingerprint.split(":")
    if len(parts) >= 2:
        return parts[1]
    return fingerprint


def _try_fpcalc(path: str) -> tuple[str, float] | None:
    """尝试用 fpcalc 命令行工具计算声纹。

    Returns:
        (base64 指纹, 时长秒) 或 None（工具不可用）
    """
    fpcalc = shutil.which("fpcalc")
    if fpcalc is None:
        return None

    try:
        result = subprocess.run(
            [fpcalc, "-length", "120", path],  # 只取前 120 秒（加速）
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return None

        fp = ""
        duration = 0.0
        for line in result.stdout.splitlines():
            if line.startswith("FINGERPRINT="):
                fp = line.split("=", 1)[1].strip()
            elif line.startswith("DURATION="):
                duration = float(line.split("=", 1)[1].strip())

        if fp:
            return (fp, duration)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass

    return None


def is_chromaprint_available() -> bool:
    """Chromaprint 是否可用（pyacoustid 或 fpcalc 任一即可）。"""
    if _HAS_ACOUSTID:
        return True
    return shutil.which("fpcalc") is not None
