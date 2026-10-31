"""媒体元数据探测（轻量、自包含）。

用于 relink Level 3 的特征兜底匹配：抽 duration + WxH + codec 做模糊比对。

为什么不直接复用主项目的 ``MediaSourceFactory.probe_video_metadata``：
    vault 是独立应用，不应强依赖 ``src/`` 的完整 import 链（那会拉进 Qt/OpenGL）。
    本模块只用 PyAV（项目已有依赖）或 subprocess ffprobe，自包含可独立运行。

优先级：
    1. PyAV（``av`` 包，自带 ffmpeg 库，无系统依赖）
    2. subprocess ffprobe（靠 PATH，用户需装 ffmpeg）
    3. 返回 None（探测失败，调用方降级）
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

# PyAV 是项目已有依赖（src/shared/media/probe.py 用它探测 ffmpeg 版本）
try:
    import av  # type: ignore
    _HAS_AV = True
except ImportError:
    _HAS_AV = False


@dataclass(frozen=True)
class MediaSignature:
    """媒体特征签名（用于 relink Level 3 模糊匹配）。

    指纹（xxhash/Chromaprint/pHash）解决"内容相同但字节变了"的场景；
    本签名解决"内容被重编码，字节和指纹都变了，但仍是同一部片"的场景——
    duration + 分辨率 + 编码格式的组合在重编码后通常不变。
    """
    duration_sec: float | None
    width: int | None
    height: int | None
    codec: str | None

    def matches(self, other: "MediaSignature", tolerance: float = 1.0) -> bool:
        """两个签名是否模糊匹配。

        Args:
            other: 另一个签名。
            tolerance: duration 容差（秒）。重编码/不同封装可能差零点几秒。

        匹配规则（全部满足才算匹配）：
            - duration 差 ≤ tolerance（视频时长是最稳定的特征）
            - width × height 完全相等（分辨率重编码通常不变）
            - codec 不要求相等（H.264 → H.265 重编码很常见）
        """
        if self.duration_sec is None or other.duration_sec is None:
            return False
        if abs(self.duration_sec - other.duration_sec) > tolerance:
            return False
        if self.width and other.width and self.width != other.width:
            return False
        if self.height and other.height and self.height != other.height:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_sec": self.duration_sec,
            "width": self.width,
            "height": self.height,
            "codec": self.codec,
        }


def probe_signature(path: str) -> MediaSignature | None:
    """探测文件的媒体特征签名。

    优先用 PyAV（自带 ffmpeg），失败回退 subprocess ffprobe，再失败返回 None。
    """
    if _HAS_AV:
        sig = _probe_with_av(path)
        if sig is not None:
            return sig

    return _probe_with_ffprobe(path)


# ---------------------------------------------------------------------- #
# 实现：PyAV
# ---------------------------------------------------------------------- #

def _probe_with_av(path: str) -> MediaSignature | None:
    """用 PyAV 探测。"""
    try:
        with av.open(path) as container:
            # 优先用视频流
            if container.streams.video:
                vs = container.streams.video[0]
                duration = float(container.duration / av.time_base) if container.duration else None
                # 某些容器 duration 在流上
                if duration is None and vs.duration is not None:
                    duration = float(vs.duration * vs.time_base)
                width = vs.width
                height = vs.height
                codec = vs.codec_context.name if hasattr(vs, "codec_context") else None
                return MediaSignature(
                    duration_sec=duration, width=width, height=height, codec=codec,
                )

            # 纯音频文件
            if container.streams.audio:
                as_ = container.streams.audio[0]
                duration = float(container.duration / av.time_base) if container.duration else None
                if duration is None and as_.duration is not None:
                    duration = float(as_.duration * as_.time_base)
                codec = as_.codec_context.name if hasattr(as_, "codec_context") else None
                return MediaSignature(
                    duration_sec=duration, width=None, height=None, codec=codec,
                )

    except Exception:
        return None

    return None


# ---------------------------------------------------------------------- #
# 实现：subprocess ffprobe
# ---------------------------------------------------------------------- #

def _locate_ffprobe() -> str | None:
    """定位 ffprobe（自包含版：打包 bin/ 目录 → PATH，不依赖 src/）。

    候选顺序与主项目 ffmpeg_locator 保持一致。
    """
    import os
    import sys
    from pathlib import Path as _Path

    exe = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    candidates: list[_Path] = []
    env_home = os.environ.get("TOPOS_FFMPEG_HOME", "").strip()
    if env_home:
        candidates.append(_Path(env_home))
    if getattr(sys, "frozen", False):
        candidates.append(_Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(_Path(meipass))
    candidates.append(_Path(__file__).resolve().parents[3])
    for directory in candidates:
        for sub in (directory, directory / "bin"):
            binary = sub / exe
            if binary.is_file():
                return str(binary)
    return shutil.which(exe)


def _probe_with_ffprobe(path: str) -> MediaSignature | None:
    """用 ffprobe 命令行探测（PyAV 不可用时的回退）。"""
    ffprobe = _locate_ffprobe()
    if ffprobe is None:
        return None

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "quiet",        # 静默（错误靠返回码判断）
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        return _parse_ffprobe_json(data)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError):
        return None


def _parse_ffprobe_json(data: dict) -> MediaSignature | None:
    """解析 ffprobe 的 JSON 输出为 MediaSignature。"""
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    # duration 在 format 级最准
    duration_str = fmt.get("duration")
    duration = float(duration_str) if duration_str else None

    # 找视频流
    for s in streams:
        if s.get("codec_type") == "video":
            width = s.get("width")
            height = s.get("height")
            codec = s.get("codec_name")
            # 流级 duration 兜底
            if duration is None:
                dur_str = s.get("duration")
                duration = float(dur_str) if dur_str else None
            return MediaSignature(
                duration_sec=duration, width=width, height=height, codec=codec,
            )

    # 找音频流（纯音频文件）
    for s in streams:
        if s.get("codec_type") == "audio":
            codec = s.get("codec_name")
            if duration is None:
                dur_str = s.get("duration")
                duration = float(dur_str) if dur_str else None
            return MediaSignature(
                duration_sec=duration, width=None, height=None, codec=codec,
            )

    return None


def is_probe_available() -> bool:
    """媒体探测是否可用（PyAV 或 ffprobe 任一即可）。"""
    if _HAS_AV:
        return True
    return _locate_ffprobe() is not None
