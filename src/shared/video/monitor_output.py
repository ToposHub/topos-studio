"""Program monitor output backends.

This module provides a first production-safe DeckLink monitoring path for the
Edit Program Viewer.  It intentionally runs out-of-band from the UI render path:
frames are copied into a tiny latest-frame queue, converted to a fixed broadcast
format, and written to an ffmpeg DeckLink output process on a stable clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
import importlib
import logging
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional, Sequence

import numpy as np
from PyQt6.QtGui import QImage
from PyQt6.QtCore import Qt

logger = logging.getLogger(__name__)
COMMON_FFMPEG_PATHS = (
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/local/bin/ffmpeg",
)


@dataclass(frozen=True)
class DeckLinkMonitorConfig:
    """Configuration for a fixed-format DeckLink program monitor output."""

    enabled: bool = False
    device: str = ""
    width: int = 1920
    height: int = 1080
    fps: float = 25.0
    mode: str = "1080i50"
    backend: str = "ffmpeg"
    format_code: str = "Hi50"
    connection: str = "hdmi"
    native_pixel_format: str = "yuv"
    input_pix_fmt: str = "bgra"
    output_pix_fmt: str = "uyvy422"
    ffmpeg_path: str = "ffmpeg"
    queue_size: int = 2
    debug: bool = False

    @classmethod
    def from_config(cls, config_manager: object | None) -> "DeckLinkMonitorConfig":
        """Build config from the app config manager, keeping DeckLink opt-in."""
        if config_manager is None:
            return cls()

        def read(key: str, default: object) -> object:
            if isinstance(config_manager, dict):
                return config_manager.get(key, default)
            get = getattr(config_manager, "get", None)
            if not callable(get):
                return default
            try:
                value = get(key)
            except TypeError:
                try:
                    value = get(key, default)
                except Exception:
                    return default
            except Exception:
                return default
            return default if value is None else value

        return cls(
            enabled=bool(read("decklink_monitor_enabled", False)),
            device=str(read("decklink_monitor_device", "") or ""),
            width=int(read("decklink_monitor_width", 1920) or 1920),
            height=int(read("decklink_monitor_height", 1080) or 1080),
            fps=float(read("decklink_monitor_fps", 25.0) or 25.0),
            mode=str(read("decklink_monitor_mode", "1080i50") or "1080i50"),
            backend=str(read("decklink_monitor_backend", "ffmpeg") or "ffmpeg"),
            format_code=str(read("decklink_monitor_format_code", "Hi50") or "Hi50"),
            connection=str(read("decklink_monitor_connection", "hdmi") or ""),
            native_pixel_format=str(read("decklink_monitor_native_pixel_format", "yuv") or "yuv"),
            input_pix_fmt=str(read("decklink_monitor_input_pix_fmt", "bgra") or "bgra"),
            output_pix_fmt=str(read("decklink_monitor_output_pix_fmt", "uyvy422") or "uyvy422"),
            ffmpeg_path=str(read("decklink_monitor_ffmpeg_path", "ffmpeg") or "ffmpeg"),
            queue_size=int(read("decklink_monitor_queue_size", 2) or 2),
            debug=bool(read("decklink_monitor_debug", False)),
        )


@dataclass(frozen=True)
class MonitorRGBAFrame:
    """Raw RGBA readback payload from a GPU framebuffer."""

    data: bytes
    width: int
    height: int
    is_gl_oriented: bool = True
    fit_mode: str = "contain"


def build_ffmpeg_decklink_command(config: DeckLinkMonitorConfig) -> list[str]:
    """Return the ffmpeg command for raw BGRA frames to DeckLink output."""
    ffmpeg_path = resolve_ffmpeg_path(config.ffmpeg_path) or config.ffmpeg_path
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pix_fmt",
        config.input_pix_fmt,
        "-s",
        f"{int(config.width)}x{int(config.height)}",
        "-r",
        _format_fps(config.fps),
        "-i",
        "pipe:0",
        "-an",
        "-f",
        "decklink",
        "-format_code",
        config.format_code,
        "-pix_fmt",
        config.output_pix_fmt,
        config.device,
    ]


def build_ffmpeg_list_devices_command(ffmpeg_path: str = "ffmpeg") -> list[str]:
    """Return a command that asks ffmpeg to list DeckLink devices."""
    ffmpeg_path = resolve_ffmpeg_path(ffmpeg_path) or ffmpeg_path
    return [
        ffmpeg_path,
        "-hide_banner",
        "-f",
        "decklink",
        "-list_devices",
        "1",
        "-i",
        "dummy",
    ]


def _format_fps(fps: float) -> str:
    if abs(float(fps) - int(float(fps))) < 1e-6:
        return str(int(float(fps)))
    return f"{float(fps):.6f}".rstrip("0").rstrip(".")


def resolve_ffmpeg_path(ffmpeg_path: str = "ffmpeg") -> Optional[str]:
    """Resolve ffmpeg even when the GUI app starts with a minimal PATH."""
    path = os.path.expandvars(str(ffmpeg_path or "ffmpeg"))
    # Accept both POSIX and Windows separators.  The old '/'-only check made
    # an explicit ``C:\\tools\\ffmpeg.exe`` go through PATH lookup and could
    # silently select a different FFmpeg installation.
    if "/" in path or "\\" in path or os.path.isabs(path):
        return path if os.path.exists(path) and os.access(path, os.X_OK) else None
    found = shutil.which(path)
    if found:
        return found
    for candidate in COMMON_FFMPEG_PATHS:
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # 打包发行：应用自带 bin/ 目录（ffmpeg_locator），未装系统 ffmpeg 也可用
    from src.shared.ffmpeg_locator import find_ffmpeg
    return find_ffmpeg()


def ffmpeg_supports_decklink(ffmpeg_path: str = "ffmpeg", timeout: float = 5.0) -> bool:
    """Return whether the ffmpeg build exposes DeckLink support."""
    resolved = resolve_ffmpeg_path(ffmpeg_path)
    if not resolved:
        return False
    try:
        completed = subprocess.run(
            [resolved, "-hide_banner", "-formats"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return False
    output = f"{completed.stdout}\n{completed.stderr}".lower()
    return "decklink" in output


# AUD-39210: ffmpeg 能力探测是子进程调用（一次最长约 5-10 秒）。
# 带结果 + TTL 的模块级缓存，避免 start/stop 循环重复探测。
_DECKLINK_SUPPORT_CACHE: dict[str, tuple[bool, float]] = {}
_DECKLINK_SUPPORT_CACHE_TTL_S = 300.0


def _decklink_support_cached(ffmpeg_path: str, timeout: float = 5.0) -> bool:
    """带 TTL 缓存的 DeckLink 支持探测（仍在调用线程执行子进程）。

    供后台线程使用；UI 线程只应调用 :func:`decklink_support_cache_lookup`
    读取已缓存的结论，绝不触发新的子进程探测。
    """
    now = time.monotonic()
    cached = _DECKLINK_SUPPORT_CACHE.get(ffmpeg_path)
    if cached is not None and cached[1] > now:
        return cached[0]
    supported = ffmpeg_supports_decklink(ffmpeg_path, timeout=timeout)
    _DECKLINK_SUPPORT_CACHE[ffmpeg_path] = (
        supported, now + _DECKLINK_SUPPORT_CACHE_TTL_S)
    return supported


def decklink_support_cache_lookup(ffmpeg_path: str) -> bool | None:
    """读取缓存的 DeckLink 支持结论（无子进程、不阻塞）。

    Returns:
        True/False 表示缓存已有结论；None 表示尚未探测过。
    """
    cached = _DECKLINK_SUPPORT_CACHE.get(ffmpeg_path)
    if cached is None or cached[1] <= time.monotonic():
        return None
    return cached[0]


def qimage_to_bgra_frame(
    image: QImage,
    *,
    width: int,
    height: int,
) -> bytes:
    """Convert a QImage to a letterboxed BGRA frame with fixed dimensions."""
    if image is None or image.isNull():
        return black_bgra_frame(width, height)

    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    src_w = int(rgb.width())
    src_h = int(rgb.height())
    if src_w <= 0 or src_h <= 0:
        return black_bgra_frame(width, height)

    ptr = rgb.bits()
    ptr.setsize(rgb.sizeInBytes())
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((src_h, rgb.bytesPerLine()))
    arr = arr[:, : src_w * 3].reshape((src_h, src_w, 3)).copy()

    target_w = max(1, int(width))
    target_h = max(1, int(height))
    scale = min(target_w / float(src_w), target_h / float(src_h))
    draw_w = max(1, int(round(src_w * scale)))
    draw_h = max(1, int(round(src_h * scale)))

    try:
        import cv2

        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        fitted = cv2.resize(arr, (draw_w, draw_h), interpolation=interpolation)
    except Exception:
        fitted_qimage = rgb.scaled(
            draw_w,
            draw_h,
            aspectRatioMode=Qt.AspectRatioMode.IgnoreAspectRatio,
            transformMode=Qt.TransformationMode.SmoothTransformation,
        )
        fitted = qimage_to_rgb_array(fitted_qimage)

    canvas = np.zeros((target_h, target_w, 4), dtype=np.uint8)
    canvas[:, :, 3] = 255
    x0 = (target_w - draw_w) // 2
    y0 = (target_h - draw_h) // 2
    canvas[y0 : y0 + draw_h, x0 : x0 + draw_w, 0] = fitted[:, :, 2]
    canvas[y0 : y0 + draw_h, x0 : x0 + draw_w, 1] = fitted[:, :, 1]
    canvas[y0 : y0 + draw_h, x0 : x0 + draw_w, 2] = fitted[:, :, 0]
    return canvas.tobytes()


def qimage_to_rgb_array(image: QImage) -> np.ndarray:
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    width = int(rgb.width())
    height = int(rgb.height())
    ptr = rgb.bits()
    ptr.setsize(rgb.sizeInBytes())
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((height, rgb.bytesPerLine()))
    return arr[:, : width * 3].reshape((height, width, 3)).copy()


def ndarray_to_bgra_frame(
    frame_array: np.ndarray,
    *,
    width: int,
    height: int,
) -> bytes:
    """Convert a BGR/BGRA-like ndarray to a letterboxed BGRA frame."""
    if frame_array is None or not isinstance(frame_array, np.ndarray) or frame_array.ndim < 2:
        return black_bgra_frame(width, height)

    array = np.asarray(frame_array)
    if array.dtype != np.uint8:
        if np.issubdtype(array.dtype, np.floating):
            array = np.clip(array * 255.0, 0, 255).astype(np.uint8)
        else:
            array = np.clip(array, 0, 255).astype(np.uint8)

    if array.ndim == 2:
        bgr = np.repeat(array[:, :, None], 3, axis=2)
    elif array.shape[2] >= 3:
        bgr = array[:, :, :3]
    else:
        return black_bgra_frame(width, height)

    src_h, src_w = bgr.shape[:2]
    if src_w <= 0 or src_h <= 0:
        return black_bgra_frame(width, height)

    target_w = max(1, int(width))
    target_h = max(1, int(height))
    scale = min(target_w / float(src_w), target_h / float(src_h))
    draw_w = max(1, int(round(src_w * scale)))
    draw_h = max(1, int(round(src_h * scale)))

    try:
        import cv2

        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        fitted = cv2.resize(bgr, (draw_w, draw_h), interpolation=interpolation)
    except Exception:
        fitted = np.ascontiguousarray(bgr)
        if (src_w, src_h) != (draw_w, draw_h):
            return black_bgra_frame(width, height)

    canvas = np.zeros((target_h, target_w, 4), dtype=np.uint8)
    canvas[:, :, 3] = 255
    x0 = (target_w - draw_w) // 2
    y0 = (target_h - draw_h) // 2
    canvas[y0 : y0 + draw_h, x0 : x0 + draw_w, 0:3] = fitted[:, :, 0:3]
    return canvas.tobytes()


def rgba_bytes_to_bgra_frame(
    raw: bytes,
    *,
    src_width: int,
    src_height: int,
    is_gl_oriented: bool,
    width: int,
    height: int,
    fit_mode: str = "contain",
) -> bytes:
    """Convert raw RGBA bytes to a fixed-size letterboxed BGRA frame."""
    try:
        rgba = np.frombuffer(raw, dtype=np.uint8).reshape((int(src_height), int(src_width), 4))
    except Exception:
        return black_bgra_frame(width, height)
    if is_gl_oriented:
        rgba = np.flipud(rgba)
    rgb = np.ascontiguousarray(rgba[:, :, :3])
    return rgb_array_to_bgra_frame(rgb, width=width, height=height, fit_mode=fit_mode)


def rgb_array_to_bgra_frame(
    rgb: np.ndarray,
    *,
    width: int,
    height: int,
    fit_mode: str = "contain",
) -> bytes:
    """Convert an RGB ndarray to a fixed-size letterboxed BGRA frame."""
    if rgb is None or not isinstance(rgb, np.ndarray) or rgb.ndim < 3 or rgb.shape[2] < 3:
        return black_bgra_frame(width, height)
    src_h, src_w = rgb.shape[:2]
    if src_w <= 0 or src_h <= 0:
        return black_bgra_frame(width, height)
    if rgb.dtype != np.uint8:
        if np.issubdtype(rgb.dtype, np.floating):
            rgb = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
        else:
            rgb = np.clip(rgb, 0, 255).astype(np.uint8)

    target_w = max(1, int(width))
    target_h = max(1, int(height))
    mode = str(fit_mode or "contain").lower()
    scale_fn = max if mode == "cover" else min
    scale = scale_fn(target_w / float(src_w), target_h / float(src_h))
    draw_w = max(1, int(round(src_w * scale)))
    draw_h = max(1, int(round(src_h * scale)))

    try:
        import cv2

        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        fitted = cv2.resize(rgb, (draw_w, draw_h), interpolation=interpolation)
    except Exception:
        fitted = np.ascontiguousarray(rgb)
        if (src_w, src_h) != (draw_w, draw_h):
            return black_bgra_frame(width, height)

    if mode == "cover":
        x0 = max(0, (draw_w - target_w) // 2)
        y0 = max(0, (draw_h - target_h) // 2)
        cropped = fitted[y0 : y0 + target_h, x0 : x0 + target_w]
        if cropped.shape[0] != target_h or cropped.shape[1] != target_w:
            return black_bgra_frame(width, height)
        canvas = np.zeros((target_h, target_w, 4), dtype=np.uint8)
        canvas[:, :, 3] = 255
        canvas[:, :, 0] = cropped[:, :, 2]
        canvas[:, :, 1] = cropped[:, :, 1]
        canvas[:, :, 2] = cropped[:, :, 0]
        return canvas.tobytes()

    canvas = np.zeros((target_h, target_w, 4), dtype=np.uint8)
    canvas[:, :, 3] = 255
    x0 = (target_w - draw_w) // 2
    y0 = (target_h - draw_h) // 2
    canvas[y0 : y0 + draw_h, x0 : x0 + draw_w, 0] = fitted[:, :, 2]
    canvas[y0 : y0 + draw_h, x0 : x0 + draw_w, 1] = fitted[:, :, 1]
    canvas[y0 : y0 + draw_h, x0 : x0 + draw_w, 2] = fitted[:, :, 0]
    return canvas.tobytes()


def bgra_frame_to_uyvy(bgra: bytes, *, width: int, height: int) -> bytes:
    """M17 · 主机侧 UYVY 4:2:2 打包（BGRA 帧字节 → UYVY 字节）。

    候选路径（真实设备裁决前的软件子项）：与向 ffmpeg 喂 BGRA 原始帧
    （由 ffmpeg swscale 转 uyvy422）相比，主机侧 cv2 向量化打包把管道
    字节减半（4K ≈ 33MB→16.5MB）并省一次 swscale。奇数宽按规格补 1
    像素（UYVY 宏像素 2 像素对齐）；失败返回原 BGRA 字节（调用方按
    input_pix_fmt=bgra 继续喂 ffmpeg——不中断监看）。
    """
    if not bgra or width <= 0 or height <= 0:
        return bgra
    try:
        import cv2

        w = int(width)
        h = int(height)
        buf = np.frombuffer(bytes(bgra), dtype=np.uint8)
        expected = w * h * 4
        if buf.size < expected:
            return bgra  # 字节不足（畸形输入）——回退 BGRA 管道
        buf = buf[:expected].reshape(h, w, 4)
        if w % 2:
            # 宏像素对齐：补 1 列（复制末列，边缘像素不引入黑边）
            buf = np.concatenate(
                [buf, buf[:, -1:, :]], axis=1)
            w += 1
        uyvy = cv2.cvtColor(np.ascontiguousarray(buf),
                            cv2.COLOR_BGRA2YUV_Y422)
        return uyvy.tobytes()
    except Exception:  # noqa: BLE001 — 打包失败回退 BGRA 管道
        return bgra


def decoded_frame_to_bgr_array(frame: object) -> np.ndarray | None:
    """Best-effort DecodedFrame conversion for monitor output background threads."""
    if frame is None:
        return None

    pixel_format = str(getattr(frame, "pixel_format", "bgra") or "bgra").lower()
    data = getattr(frame, "data", None)
    bit_depth = int(getattr(frame, "bit_depth", 8) or 8)
    if isinstance(data, np.ndarray):
        if pixel_format in {"nv12", "nv21"} and data.ndim == 2 and data.dtype == np.uint8:
            # AUD-39211: NV12 此前硬走 cv2 的 BT.601 转换，HD（BT.709）
            # 素材外部监看颜色偏移。改为拆平面后按帧携带的色彩元数据
            # 走与 planar 路径相同的矩阵感知转换。
            frame_h = (data.shape[0] // 3) * 2
            if frame_h <= 0 or data.shape[1] <= 0 or data.shape[0] - frame_h < 1:
                return None
            y_plane = data[:frame_h]
            uv_plane = data[frame_h:]
            try:
                # NV12 布局：Y 占前 frame_h 行（宽 w），UV 交错占余下
                # frame_h/2 行（每行 w 字节 = w/2 个 (U,V) 对）
                uv = uv_plane.reshape(frame_h // 2, -1, 2)
            except ValueError:
                return None
            if uv.shape[1] * 2 != y_plane.shape[1]:
                return None
            if pixel_format == "nv12":
                u_plane, v_plane = uv[:, :, 0], uv[:, :, 1]
            else:  # nv21
                v_plane, u_plane = uv[:, :, 0], uv[:, :, 1]
            u_up = _upsample_chroma(u_plane, y_plane.shape[0], y_plane.shape[1])
            v_up = _upsample_chroma(v_plane, y_plane.shape[0], y_plane.shape[1])
            if u_up is None or v_up is None:
                return None
            return _yuv_planes_to_bgr(
                y_plane, u_up, v_up,
                getattr(frame, "color_matrix", None),
                getattr(frame, "color_range", None),
            )

        if data.ndim >= 3 and data.shape[2] >= 3:
            if data.dtype == np.uint16:
                if bit_depth > 0 and bit_depth < 16:
                    scale = float((1 << bit_depth) - 1)
                else:
                    scale = 65535.0
                channels = np.clip(data[:, :, :3].astype(np.float32) / scale * 255.0, 0, 255).astype(np.uint8)
            elif data.dtype == np.uint8:
                channels = data[:, :, :3]
            elif np.issubdtype(data.dtype, np.floating):
                channels = np.clip(data[:, :, :3] * 255.0, 0, 255).astype(np.uint8)
            else:
                channels = np.clip(data[:, :, :3], 0, 255).astype(np.uint8)

            if pixel_format.startswith("rgb") or pixel_format.startswith("rgba") or pixel_format.startswith("gbr"):
                return np.ascontiguousarray(channels[:, :, ::-1])
            return np.ascontiguousarray(channels)

    planes = getattr(frame, "planes", None)
    if not planes:
        return None
    try:
        if pixel_format.startswith("gbr") and len(planes) >= 3:
            g = _plane_to_uint8(planes[0], bit_depth)
            b = _plane_to_uint8(planes[1], bit_depth)
            r = _plane_to_uint8(planes[2], bit_depth)
            return np.dstack([b, g, r]).astype(np.uint8, copy=False)
        if len(planes) >= 3:
            y = _plane_to_uint8(planes[0], bit_depth)
            u = _plane_to_uint8(planes[1], bit_depth)
            v = _plane_to_uint8(planes[2], bit_depth)
            if u.shape[:2] != y.shape[:2]:
                u_up = _upsample_chroma(u, y.shape[0], y.shape[1])
                v_up = _upsample_chroma(v, y.shape[0], y.shape[1])
                if u_up is None or v_up is None:
                    return None
                u, v = u_up, v_up
            # AUD-39211: 按帧色彩元数据选择转换矩阵（不再硬编码单一
            # 系数集），HD/SD/HDR-WCG 素材外部监看不再颜色偏移。
            return _yuv_planes_to_bgr(
                y, u, v,
                getattr(frame, "color_matrix", None),
                getattr(frame, "color_range", None),
            )
    except Exception:
        return None
    return None


def _upsample_chroma(plane: np.ndarray, height: int, width: int) -> np.ndarray | None:
    """上采样色度平面到 luma 分辨率（cv2 优先，缺失时 numpy 最近邻）。"""
    if plane.shape[0] == height and plane.shape[1] == width:
        return plane
    try:
        import cv2

        return cv2.resize(
            plane, (width, height), interpolation=cv2.INTER_LINEAR)
    except Exception:
        try:
            rep_h = max(1, height // plane.shape[0])
            rep_w = max(1, width // plane.shape[1])
            return np.repeat(np.repeat(plane, rep_h, axis=0), rep_w, axis=1)
        except ValueError:
            return None


# AUD-39211: YUV→BGR 转换系数表（全量程 8-bit 形式：
# R = Y + kr_v*V'；G = Y - kg_u*U' - kg_v*V'；B = Y + kb_u*U'，
# U'/V' 为已居中的色度）。旧实现硬编码单一系数集（= BT.709 全量程），
# SD（BT.601）与 limited-range 素材外部监看颜色偏移。
_YUV_FULL_RANGE_COEFFS: dict[str, tuple[float, float, float, float]] = {
    # (kr_v, kg_u, kg_v, kb_u)
    "bt709": (1.5748, 0.1873, 0.4681, 1.8556),
    "bt601": (1.402, 0.3441, 0.7141, 1.772),
    "smpte170m": (1.402, 0.3441, 0.7141, 1.772),  # BT.601 (NTSC)
    "bt470bg": (1.402, 0.3441, 0.7141, 1.772),    # BT.601 (PAL)
    "bt2020nc": (1.4746, 0.1646, 0.5715, 1.8814),
    "bt2020": (1.4746, 0.1646, 0.5715, 1.8814),
}


def _yuv_planes_to_bgr(
    y_plane: np.ndarray,
    u_plane: np.ndarray,
    v_plane: np.ndarray,
    color_matrix: object,
    color_range: object,
) -> np.ndarray:
    """按帧色彩元数据把 8-bit YUV 平面转换为 BGR（AUD-39211）。

    - color_matrix/color_range 完全缺失：保持旧行为（BT.709 全量程
      系数、无范围扩展）——无依据不改变既有输出。
    - 元数据存在：按 normalize 后的矩阵选择系数；limited 范围先做
      16-235/16-240 → 全量程扩展再应用全量程系数。
    最终 clip 到 [0, 255] 仅是 uint8 输出的量化边界，不做其他钳制。

    Args:
        y_plane: 8-bit Y 平面（已归一化到 0-255 尺度）
        u_plane: 8-bit U（Cb）平面（与 Y 同分辨率）
        v_plane: 8-bit V（Cr）平面（与 Y 同分辨率）
        color_matrix: 帧色彩矩阵（如 'bt709'；None 表示未知）
        color_range: 帧色彩范围（'limited'/'full'；None 表示未知）

    Returns:
        BGR uint8 数组
    """
    y = np.asarray(y_plane, dtype=np.float32)
    u = np.asarray(u_plane, dtype=np.float32) - 128.0
    v = np.asarray(v_plane, dtype=np.float32) - 128.0

    matrix_text = str(color_matrix).strip().lower() if color_matrix is not None else ""
    range_text = str(color_range).strip().lower() if color_range is not None else ""

    if not matrix_text and not range_text:
        # 元数据完全缺失 → 旧行为
        coeffs = _YUV_FULL_RANGE_COEFFS["bt709"]
        y_full = y
    else:
        from src.shared.media.gpu_frame import normalize_color_matrix, normalize_color_range

        matrix = normalize_color_matrix(matrix_text or None)
        coeffs = _YUV_FULL_RANGE_COEFFS.get(matrix, _YUV_FULL_RANGE_COEFFS["bt709"])
        if normalize_color_range(range_text or None) == "limited":
            y_full = (y - 16.0) * (255.0 / 219.0)
            u = u * (255.0 / 224.0)
            v = v * (255.0 / 224.0)
        else:
            y_full = y

    kr_v, kg_u, kg_v, kb_u = coeffs
    b = y_full + kb_u * u
    g = y_full - kg_u * u - kg_v * v
    r = y_full + kr_v * v
    return np.clip(np.dstack([b, g, r]), 0, 255).astype(np.uint8)


def _plane_to_uint8(plane: np.ndarray, bit_depth: int) -> np.ndarray:
    if plane.dtype == np.uint8:
        return plane
    if np.issubdtype(plane.dtype, np.integer):
        max_value = float((1 << max(1, int(bit_depth))) - 1)
        return np.clip(plane.astype(np.float32) / max_value * 255.0, 0, 255).astype(np.uint8)
    if np.issubdtype(plane.dtype, np.floating):
        return np.clip(plane * 255.0, 0, 255).astype(np.uint8)
    return np.clip(plane, 0, 255).astype(np.uint8)


def monitor_payload_to_bgra_frame(payload: object, *, width: int, height: int) -> bytes:
    """Convert a QImage, ndarray, or DecodedFrame-like object to BGRA."""
    if isinstance(payload, QImage):
        return qimage_to_bgra_frame(payload, width=width, height=height)
    if isinstance(payload, MonitorRGBAFrame):
        return rgba_bytes_to_bgra_frame(
            payload.data,
            src_width=payload.width,
            src_height=payload.height,
            is_gl_oriented=payload.is_gl_oriented,
            width=width,
            height=height,
            fit_mode=getattr(payload, "fit_mode", "contain"),
        )
    if isinstance(payload, np.ndarray):
        return ndarray_to_bgra_frame(payload, width=width, height=height)
    frame_array = decoded_frame_to_bgr_array(payload)
    if frame_array is not None:
        return ndarray_to_bgra_frame(frame_array, width=width, height=height)
    return black_bgra_frame(width, height)


def monitor_payload_size(payload: object) -> tuple[int | None, int | None]:
    if isinstance(payload, QImage):
        return int(payload.width()), int(payload.height())
    if isinstance(payload, MonitorRGBAFrame):
        return int(payload.width), int(payload.height)
    if isinstance(payload, np.ndarray) and payload.ndim >= 2:
        return int(payload.shape[1]), int(payload.shape[0])
    width = int(getattr(payload, "width", 0) or 0)
    height = int(getattr(payload, "height", 0) or 0)
    if width > 0 and height > 0:
        return width, height
    data = getattr(payload, "data", None)
    if isinstance(data, np.ndarray) and data.ndim >= 2:
        return int(data.shape[1]), int(data.shape[0])
    planes = getattr(payload, "planes", None)
    if planes and isinstance(planes[0], np.ndarray) and planes[0].ndim >= 2:
        return int(planes[0].shape[1]), int(planes[0].shape[0])
    return None, None


def black_bgra_frame(width: int, height: int) -> bytes:
    frame = np.zeros((max(1, int(height)), max(1, int(width)), 4), dtype=np.uint8)
    frame[:, :, 3] = 255
    return frame.tobytes()


class FfmpegDeckLinkMonitorOutput:
    """Stable-clock rawvideo writer for ffmpeg's DeckLink output backend."""

    def __init__(
        self,
        config: DeckLinkMonitorConfig,
        *,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._process_factory = process_factory
        self._monotonic = monotonic
        self._sleep = sleep
        self._queue: queue.Queue[tuple[int, Any]] = queue.Queue(
            maxsize=max(1, int(config.queue_size))
        )
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._process: Optional[subprocess.Popen] = None
        self._last_frame: bytes = black_bgra_frame(config.width, config.height)
        self._last_frame_index: Optional[int] = None
        self._started = False
        # AUD-39214: 丢帧计数被提交线程与写线程并发自增（读-改-写），
        # 加锁防丢计数。
        self._drop_counter_lock = threading.Lock()
        self._dropped_frames = 0

    @property
    def is_running(self) -> bool:
        return self._started and self._thread is not None and self._thread.is_alive()

    @property
    def dropped_frames(self) -> int:
        # AUD-39214: 与自增互斥读取
        with self._drop_counter_lock:
            return self._dropped_frames

    def start(self) -> bool:
        """Start the writer thread.

        AUD-39210: 此前 start() 在调用线程同步执行 ffmpeg 支持探测与
        DeckLink 设备枚举（两个子进程，合计最长约 10-15 秒），从 UI 线程
        调用会把界面卡死。现在 start() 只做廉价的本地检查（ffmpeg 路径
        解析、缓存的探测结论），子进程探测全部移到后台 ``_run`` 线程在
        启动 ffmpeg 前完成；探测失败以日志呈现，``is_running`` 随线程
        退出转 False。
        """
        if not self.config.enabled:
            return False
        ffmpeg_path = resolve_ffmpeg_path(self.config.ffmpeg_path)
        if not ffmpeg_path:
            logger.warning(
                "DeckLink monitor output disabled: ffmpeg not found. "
                "Set Preferences > External Video Monitoring > ffmpeg path."
            )
            return False
        # 只读缓存结论：命中"不支持"可立即失败（保持 native 后端回退），
        # 未命中时不在本线程探测。
        cached_support = decklink_support_cache_lookup(ffmpeg_path)
        if cached_support is False:
            logger.warning(
                "DeckLink monitor output disabled: ffmpeg at %s was found, "
                "but this build does not include DeckLink support. Use a "
                "DeckLink-enabled ffmpeg build or the Native SDK backend.",
                ffmpeg_path,
            )
            return False

        if self.config.ffmpeg_path != ffmpeg_path:
            self.config = replace(self.config, ffmpeg_path=ffmpeg_path)
        if self.is_running:
            return True

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="DeckLinkMonitorOutput",
            daemon=True,
        )
        self._thread.start()
        self._started = True
        return True

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None
        self._started = False
        self._terminate_process()

    def submit_qimage(self, frame_index: int, image: QImage) -> bool:
        if not self.is_running or image is None or image.isNull():
            return False
        return self.submit_frame_payload(frame_index, image.copy())

    def submit_frame_payload(self, frame_index: int, payload: object) -> bool:
        if not self.is_running or payload is None:
            return False
        item = (int(frame_index), payload)
        while self._queue.full():
            try:
                self._queue.get_nowait()
                # AUD-39214: 并发自增持锁，防丢计数
                with self._drop_counter_lock:
                    self._dropped_frames += 1
            except queue.Empty:
                break
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            with self._drop_counter_lock:
                self._dropped_frames += 1
            return False

    def _run(self) -> None:
        # AUD-39210: 子进程探测（ffmpeg 能力 + 设备枚举）在后台线程执行，
        # 不再阻塞调用 start() 的线程。
        ffmpeg_path = resolve_ffmpeg_path(self.config.ffmpeg_path)
        if not ffmpeg_path:
            logger.warning(
                "DeckLink monitor output stopped: ffmpeg disappeared. "
                "Set Preferences > External Video Monitoring > ffmpeg path."
            )
            return
        if not _decklink_support_cached(ffmpeg_path):
            logger.warning(
                "DeckLink monitor output stopped: ffmpeg at %s was found, "
                "but this build does not include DeckLink support. Use a "
                "DeckLink-enabled ffmpeg build or the Native SDK backend.",
                ffmpeg_path,
            )
            return

        config = self.config
        if not config.device:
            devices = list_decklink_devices(ffmpeg_path)
            if len(devices) == 1:
                config = replace(config, device=devices[0], ffmpeg_path=ffmpeg_path)
                self.config = config
                logger.info("Auto-selected DeckLink monitor device: %s", devices[0])
            elif devices:
                logger.warning(
                    "DeckLink monitor output is enabled but no device is configured. "
                    "Detected devices: %s",
                    ", ".join(devices),
                )
                return
            else:
                logger.warning(
                    "DeckLink monitor output is enabled but no device is configured, "
                    "and ffmpeg could not list any DeckLink devices."
                )
                return
        elif config.ffmpeg_path != ffmpeg_path:
            config = replace(config, ffmpeg_path=ffmpeg_path)
            self.config = config

        command = build_ffmpeg_decklink_command(config)
        logger.info("Starting DeckLink monitor output: %s", " ".join(command))
        try:
            self._process = self._process_factory(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as exc:
            logger.warning("Failed to start DeckLink monitor output: %s", exc)
            return

        # AUD-39209: stderr=PIPE 必须有读取方。ffmpeg 一旦写满管道缓冲
        # （约 64KB）就会阻塞在 stderr 写入上、停止消费 stdin，监视器
        # 输出随之冻结。专用 daemon 线程持续排空 stderr（节流落日志）。
        stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(getattr(self._process, "stderr", None),),
            name="DeckLinkMonitorStderr",
            daemon=True,
        )
        stderr_thread.start()

        interval = 1.0 / max(1.0, float(self.config.fps))
        next_time = self._monotonic()
        while not self._stop_event.is_set():
            self._drain_latest_frame()
            if not self._write_frame(self._last_frame):
                break
            next_time += interval
            delay = next_time - self._monotonic()
            if delay > 0:
                self._sleep(delay)
            else:
                next_time = self._monotonic()

        self._terminate_process()
        if stderr_thread is not None and stderr_thread.is_alive():
            stderr_thread.join(timeout=0.5)

    def _drain_stderr(self, stream: Any) -> None:
        """持续读取 ffmpeg stderr 直到进程退出（AUD-39209）。"""
        if stream is None:
            return
        last_log = 0.0
        try:
            for line in iter(stream.readline, b""):
                if not line:
                    break
                now = time.monotonic()
                if now - last_log > 2.0:
                    last_log = now
                    logger.debug(
                        "DeckLink ffmpeg stderr: %s",
                        line.decode("utf-8", errors="replace").rstrip(),
                    )
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _drain_latest_frame(self) -> None:
        latest: Optional[tuple[int, Any]] = None
        while True:
            try:
                latest = self._queue.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        frame_index, payload = latest
        self._last_frame = monitor_payload_to_bgra_frame(
            payload,
            width=self.config.width,
            height=self.config.height,
        )
        self._last_frame_index = int(frame_index)

    def _write_frame(self, frame: bytes) -> bool:
        process = self._process
        stdin = getattr(process, "stdin", None)
        if process is None or stdin is None:
            return False
        try:
            stdin.write(frame)
            flush = getattr(stdin, "flush", None)
            if callable(flush):
                flush()
            return True
        except (BrokenPipeError, OSError, ValueError) as exc:
            logger.warning("DeckLink monitor output stopped while writing frame: %s", exc)
            return False

    def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        stdin = getattr(process, "stdin", None)
        if stdin is not None:
            try:
                stdin.close()
            except Exception:
                pass
        poll = getattr(process, "poll", None)
        terminate = getattr(process, "terminate", None)
        wait = getattr(process, "wait", None)
        kill = getattr(process, "kill", None)
        try:
            if callable(poll) and poll() is None and callable(terminate):
                terminate()
            if callable(wait):
                wait(timeout=1.0)
        except Exception:
            if callable(kill):
                try:
                    kill()
                except Exception:
                    pass

    def _resolve_ffmpeg(self) -> bool:
        return resolve_ffmpeg_path(self.config.ffmpeg_path) is not None


class NativeDeckLinkSDKMonitorOutput:
    """Optional native DeckLink SDK backend.

    Python cannot call the DeckLink C++ scheduled playback API or share GL
    textures with DeckLink directly.  This backend loads a project-provided
    extension module named ``topos_decklink`` that owns the SDK integration.

    Expected extension API:
        create_output(device, width, height, fps, mode) -> output
        output.start()
        output.submit_gpu_texture(texture_id, width, height, frame_index, fence_ns, is_gl_oriented)
        output.submit_bgra(frame_bytes, width, height, frame_index)  # optional fallback
        output.stop()
    """

    def __init__(
        self,
        config: DeckLinkMonitorConfig,
        *,
        module_name: str = "topos_decklink",
    ) -> None:
        self.config = config
        self.module_name = module_name
        self._module = None
        self._output = None
        self._running = False
        self._supports_gpu_texture = False
        self._queue: queue.Queue[tuple[int, Any]] = queue.Queue(
            maxsize=max(1, int(config.queue_size))
        )
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # AUD-39214: 丢帧计数被提交调用方线程与 submitter 线程并发自增，
        # 加锁防丢计数。
        self._drop_counter_lock = threading.Lock()
        self._dropped_frames = 0
        self._submitted_frames = 0
        self._last_status_log = 0.0
        self._last_submit_error_log = 0.0
        self._last_debug_log_by_event: dict[str, float] = {}
        self._last_content_debug_log = 0.0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def supports_gpu_texture(self) -> bool:
        return self._supports_gpu_texture

    def start(self) -> bool:
        if not self.config.enabled:
            return False
        try:
            self._module = self._import_bridge_module()
            config = self.config
            if not config.device:
                list_devices = getattr(self._module, "list_devices", None)
                devices = list(list_devices()) if callable(list_devices) else []
                if len(devices) == 1:
                    config = replace(config, device=str(devices[0]))
                    self.config = config
                    logger.info("Auto-selected native DeckLink monitor device: %s", config.device)
                elif devices:
                    logger.warning(
                        "Native DeckLink monitor output is enabled but no device is configured. "
                        "Detected devices: %s",
                        ", ".join(str(device) for device in devices),
                    )
                    return False
                else:
                    logger.warning(
                        "Native DeckLink monitor output is enabled but no device is configured, "
                        "and the SDK bridge could not list any DeckLink devices."
                    )
                    return False
            factory = getattr(self._module, "create_output")
            self._output = factory(
                device=config.device,
                width=int(config.width),
                height=int(config.height),
                fps=float(config.fps),
                mode=config.mode,
                connection=config.connection,
                pixel_format=config.native_pixel_format,
            )
            self._supports_gpu_texture = bool(
                getattr(self._output, "supports_gpu_texture", False)
            )
            start = getattr(self._output, "start", None)
            if callable(start):
                start()
            self._log_status("started", force=True)
        except Exception as exc:
            logger.warning("Native DeckLink SDK backend unavailable: %s", exc)
            self._output = None
            self._running = False
            self._supports_gpu_texture = False
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_bgra_submitter,
            name="NativeDeckLinkSubmitter",
            daemon=True,
        )
        self._thread.start()
        self._running = True
        return True

    def _import_bridge_module(self):
        try:
            return importlib.import_module(self.module_name)
        except ModuleNotFoundError as exc:
            if exc.name != self.module_name:
                raise
            src_root = Path(__file__).resolve().parents[2]
            src_root_text = str(src_root)
            if src_root_text not in sys.path:
                sys.path.insert(0, src_root_text)
            return importlib.import_module(self.module_name)

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None
        output = self._output
        self._output = None
        self._running = False
        self._supports_gpu_texture = False
        stop = getattr(output, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass

    def submit_qimage(self, frame_index: int, image: QImage) -> bool:
        if not self.is_running or image is None or image.isNull():
            self._debug_log(
                "native_qimage_rejected",
                frame=frame_index,
                running=self.is_running,
                image_null=image is None or image.isNull(),
                throttle=2.0,
            )
            return False
        return self.submit_frame_payload(frame_index, image.copy())

    def submit_frame_payload(self, frame_index: int, payload: object) -> bool:
        if not self.is_running or payload is None:
            self._debug_log(
                "native_payload_rejected",
                frame=frame_index,
                running=self.is_running,
                payload_type=type(payload).__name__,
                throttle=2.0,
            )
            return False
        item = (int(frame_index), payload)
        input_width, input_height = monitor_payload_size(payload)
        input_size = f"{input_width}x{input_height}" if input_width and input_height else "unknown"
        while self._queue.full():
            try:
                self._queue.get_nowait()
                # AUD-39214: 并发自增持锁，防丢计数
                with self._drop_counter_lock:
                    self._dropped_frames += 1
            except queue.Empty:
                break
        try:
            self._queue.put_nowait(item)
            self._debug_log(
                "native_payload_enqueued",
                frame=frame_index,
                input=input_size,
                payload_type=type(payload).__name__,
                output=f"{self.config.width}x{self.config.height}",
                fps=self.config.fps,
                mode=self.config.mode,
                connection=self.config.connection,
                pixel_format=self.config.native_pixel_format,
                queue_size=self._queue.qsize(),
                dropped=self._dropped_frames,
                throttle=1.0,
            )
            return True
        except queue.Full:
            with self._drop_counter_lock:
                self._dropped_frames += 1
            self._debug_log(
                "native_payload_rejected",
                frame=frame_index,
                reason="queue_full",
                dropped=self._dropped_frames,
                throttle=1.0,
            )
            return False

    def _run_bgra_submitter(self) -> None:
        while not self._stop_event.is_set():
            latest: Optional[tuple[int, Any]] = None
            try:
                latest = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            while True:
                try:
                    latest = self._queue.get_nowait()
                    # AUD-39214: 与提交线程并发自增，持锁防丢计数
                    with self._drop_counter_lock:
                        self._dropped_frames += 1
                except queue.Empty:
                    break
            if latest is None:
                continue
            frame_index, payload = latest
            output = self._output
            submit = getattr(output, "submit_bgra", None)
            if output is None or not callable(submit):
                continue
            try:
                frame = monitor_payload_to_bgra_frame(
                    payload,
                    width=self.config.width,
                    height=self.config.height,
                )
                self._debug_frame_content(frame, frame_index)
                submit(frame, int(self.config.width), int(self.config.height), int(frame_index))
                self._submitted_frames += 1
                self._debug_log(
                    "native_bgra_submitted",
                    frame=frame_index,
                    bytes=len(frame),
                    output=f"{self.config.width}x{self.config.height}",
                    fps=self.config.fps,
                    mode=self.config.mode,
                    submitted=self._submitted_frames,
                    dropped=self._dropped_frames,
                    throttle=1.0,
                )
                self._log_status("submit")
            except Exception as exc:
                now = time.monotonic()
                if now - self._last_submit_error_log > 2.0:
                    self._last_submit_error_log = now
                    logger.warning("Native DeckLink BGRA submit failed: %s", exc)
                else:
                    logger.debug("Native DeckLink BGRA submit failed: %s", exc)

    def _log_status(self, reason: str, *, force: bool = False) -> None:
        if not self.config.debug:
            return
        status_getter = getattr(self._output, "status", None)
        if not callable(status_getter):
            return
        now = time.monotonic()
        if not force and now - self._last_status_log < 2.0:
            return
        self._last_status_log = now
        try:
            status = status_getter()
        except Exception:
            return
        submitted = int(status.get("submitted_frames", 0) or 0)
        completed = int(status.get("completed_frames", 0) or 0)
        failures = int(status.get("schedule_failures", 0) or 0)
        late = int(status.get("late_frames", 0) or 0)
        logger.info(
            "[DECKLINK_MONITOR_DEBUG] Native DeckLink monitor status reason=%s running=%s submitted=%s "
            "scheduled=%s completed=%s late=%s failures=%s device=%s",
            reason,
            status.get("running"),
            submitted,
            status.get("scheduled_frames"),
            completed,
            late,
            failures,
            status.get("device"),
        )

    def _debug_frame_content(self, frame: bytes, frame_index: int) -> None:
        if not self.config.debug:
            return
        now = time.monotonic()
        if now - self._last_content_debug_log < 2.0:
            return
        self._last_content_debug_log = now
        try:
            sample = np.frombuffer(frame, dtype=np.uint8)
            if sample.size < 4:
                return
            pixels = sample.reshape((-1, 4))
            stride = max(1, pixels.shape[0] // 4096)
            sampled = pixels[::stride, :3]
            mean_bgr = sampled.mean(axis=0)
            nonblack_ratio = float(np.mean(np.any(sampled > 8, axis=1)))
            logger.info(
                "[DECKLINK_MONITOR_DEBUG] event=native_bgra_content frame=%s "
                "mean_bgr=%.1f,%.1f,%.1f nonblack=%.3f",
                frame_index,
                float(mean_bgr[0]),
                float(mean_bgr[1]),
                float(mean_bgr[2]),
                nonblack_ratio,
            )
        except Exception as exc:
            logger.debug("Native DeckLink content debug failed: %s", exc)

    def _debug_log(self, event: str, *, throttle: float = 1.0, **fields) -> None:
        if not self.config.debug:
            return
        now = time.monotonic()
        last = float(self._last_debug_log_by_event.get(event, 0.0) or 0.0)
        if now - last < throttle:
            return
        self._last_debug_log_by_event[event] = now
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.info("[DECKLINK_MONITOR_DEBUG] event=%s %s", event, detail)

    def submit_gpu_frame(self, frame: NativeGpuMonitorFrame) -> bool:
        output = self._output
        submit = getattr(output, "submit_gpu_texture", None)
        if not self.is_running or not callable(submit):
            return False
        try:
            submit(
                int(frame.texture_id),
                int(frame.width),
                int(frame.height),
                int(frame.frame_index),
                int(frame.fence_ns),
                bool(frame.is_gl_oriented),
            )
            return True
        except Exception as exc:
            logger.debug("Native DeckLink GPU submit failed: %s", exc)
            return False


class ProgramMonitorOutputController:
    """High-level program monitor output controller used by the Edit page."""

    def __init__(
        self,
        config: DeckLinkMonitorConfig,
        *,
        backend: Optional[object] = None,
    ) -> None:
        self.config = config
        self.backend = backend or self._create_backend(config)
        self._last_debug_log_by_event: dict[str, float] = {}

    @classmethod
    def from_config(cls, config_manager: object | None) -> "ProgramMonitorOutputController":
        return cls(DeckLinkMonitorConfig.from_config(config_manager))

    @staticmethod
    def _create_backend(config: DeckLinkMonitorConfig) -> object:
        if str(config.backend).lower() in {"native", "native_sdk", "sdk"}:
            return NativeDeckLinkSDKMonitorOutput(config)
        return FfmpegDeckLinkMonitorOutput(config)

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self.backend.start():
            return True

        if isinstance(self.backend, NativeDeckLinkSDKMonitorOutput):
            return False

        native_backend = NativeDeckLinkSDKMonitorOutput(
            replace(self.config, backend="native_sdk")
        )
        if native_backend.start():
            logger.info(
                "Using Native DeckLink SDK monitor backend after ffmpeg backend failed"
            )
            self.backend = native_backend
            return True
        return False

    def stop(self) -> None:
        self.backend.stop()

    def submit_frame(self, frame_index: int, result: object) -> bool:
        if not self.backend.is_running:
            self._debug_log(
                "controller_submit_rejected",
                reason="backend_not_running",
                frame=frame_index,
                backend=type(self.backend).__name__,
                throttle=1.0,
            )
            return False
        if not isinstance(result, QImage):
            self._debug_log(
                "controller_submit_rejected",
                reason="non_qimage",
                frame=frame_index,
                result_type=type(result).__name__,
                backend=type(self.backend).__name__,
                throttle=1.0,
            )
            return False
        self._debug_log(
            "controller_submit_qimage",
            frame=frame_index,
            input=f"{result.width()}x{result.height()}",
            output=f"{self.config.width}x{self.config.height}",
            fps=self.config.fps,
            mode=self.config.mode,
            backend=type(self.backend).__name__,
            throttle=1.0,
        )
        return self.backend.submit_qimage(frame_index, result)

    def submit_video_frame(self, frame_index: int, payload: object) -> bool:
        """Submit a decoded video payload for background monitor conversion."""
        if not self.backend.is_running:
            self._debug_log(
                "controller_submit_rejected",
                reason="backend_not_running",
                frame=frame_index,
                backend=type(self.backend).__name__,
                throttle=1.0,
            )
            return False
        submit = getattr(self.backend, "submit_frame_payload", None)
        if not callable(submit):
            if isinstance(payload, QImage):
                return self.backend.submit_qimage(frame_index, payload)
            self._debug_log(
                "controller_submit_rejected",
                reason="payload_unsupported",
                frame=frame_index,
                payload_type=type(payload).__name__,
                backend=type(self.backend).__name__,
                throttle=1.0,
            )
            return False
        input_width, input_height = monitor_payload_size(payload)
        self._debug_log(
            "controller_submit_payload",
            frame=frame_index,
            input=f"{input_width}x{input_height}" if input_width and input_height else "unknown",
            output=f"{self.config.width}x{self.config.height}",
            fps=self.config.fps,
            mode=self.config.mode,
            backend=type(self.backend).__name__,
            payload_type=type(payload).__name__,
            throttle=1.0,
        )
        return bool(submit(frame_index, payload))

    def submit_gpu_frame(self, frame: NativeGpuMonitorFrame) -> bool:
        submit = getattr(self.backend, "submit_gpu_frame", None)
        if not self.backend.is_running or not callable(submit):
            return False
        return bool(submit(frame))

    def _debug_log(self, event: str, *, throttle: float = 1.0, **fields) -> None:
        if not self.config.debug:
            return
        now = time.monotonic()
        last = float(self._last_debug_log_by_event.get(event, 0.0) or 0.0)
        if now - last < throttle:
            return
        self._last_debug_log_by_event[event] = now
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.info("[DECKLINK_MONITOR_DEBUG] event=%s %s", event, detail)


def list_decklink_devices(ffmpeg_path: str = "ffmpeg", timeout: float = 5.0) -> Sequence[str]:
    """Best-effort DeckLink device discovery through ffmpeg."""
    resolved = resolve_ffmpeg_path(ffmpeg_path)
    if not resolved or not ffmpeg_supports_decklink(resolved):
        return []
    command = build_ffmpeg_list_devices_command(resolved)
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        logger.debug("Failed to list DeckLink devices: %s", exc)
        return []

    devices: list[str] = []
    for line in (completed.stderr or "").splitlines():
        marker = "'"
        if "DeckLink" not in line or marker not in line:
            continue
        parts = line.split(marker)
        if len(parts) >= 3 and parts[1] and parts[1] not in devices:
            devices.append(parts[1])
    return devices
@dataclass(frozen=True)
class NativeGpuMonitorFrame:
    """GPU-resident frame metadata for a native DeckLink SDK bridge."""

    frame_index: int
    texture_id: int
    width: int
    height: int
    fence_ns: int = 0
    is_gl_oriented: bool = True
