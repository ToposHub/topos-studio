"""标准图像序列编码器（G14，第六轮审计能力建设；R15–R19 第七轮加固）。

PNG/TIFF 逐帧序列交付：外部合成/动画软件（Nuke/AE/Resolve）可直接回导
的标准命名 PNG（rgb24/rgba，8-bit；rgb48be/rgba64be，16-bit）与 TIFF
（rgb24/rgb48le/rgba/rgba64le，8/16-bit）。

第七轮审计加固：
- R15 文件安全：帧先写进隐藏暂存目录，成功 close 才逐帧原子提交到目标
  目录；同名旧序列默认冲突即失败（不覆盖），显式授权覆盖时才替换并
  清理同基名的旧尾帧（长→短重导出不混帧）；取消只清本次暂存，旧文件
  哈希不变。
- R16 目录语义：``output_path`` 指向已存在目录时，帧写进该目录内部
  （此前写到父目录、以目录名当基名）。
- R17 作业配置：位深（8/16）、帧号 padding、首帧号、覆盖策略显式来自
  ``TimelineExportConfig``（``bit_depth>=16`` 时渲染管线走高精度 uint16
  路径——真实 16-bit，非 8bit 上采样）。
- R18 PNG16 端序：PyAV ``from_ndarray`` 对 be 格式自行做字节序转换，
  直接传原生死序数组；手动 ``byteswap`` 会二次交换损坏像素值。
- R19 音频能力声明：``supports_audio = False``——主导出据此显式跳过
  音频并把 ``audio_omitted=True`` 披露到结果/队列，而不是尝试不存在的
  ``add_audio_stream`` 后静默成功。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from typing import Any, Dict, List, Optional

from src.shared.color.project_video_signal import ACES_EXR_CHROMATICITIES

logger = logging.getLogger(__name__)

_SAN_BASE_RE = re.compile(r'[\\/:*?"<>|]+')
_STAGE_SUFFIX = ".topos_stage"
_SEQUENCE_FORMATS = {
    "png_sequence": ("png", ".png"),
    "tiff_sequence": ("tiff", ".tif"),
    "exr_sequence": ("exr", ".exr"),
}


def _sanitize_base(name: str) -> str:
    """序列文件名基名只保留文件系统安全字符。"""
    return _SAN_BASE_RE.sub("_", name).strip(" ._") or "sequence"


class StandardImageSequenceEncoder:
    """PNG/TIFF/EXR 序列编码器（encoder 契约与 FFmpegVideoEncoder 兼容）。

    契约方法：``open()`` / ``encode_frame(DecodedFrame)`` /
    ``write_audio_samples(...)``（no-op 警告）/ ``close()`` / ``abort()``。
    """

    # R19：图像序列不承载音频——主导出据此设置 audio_omitted。
    supports_audio = False

    def __init__(
        self, output_path: str, config: Any, *, icc_profile: bytes | None = None,
        exr_space: str | None = None,
    ) -> None:
        self._config = config
        codec = str(getattr(config, "codec", "png_sequence")).lower()
        if codec not in _SEQUENCE_FORMATS:
            raise ValueError(f"not an image-sequence codec: {codec}")
        self._codec_name, self._ext = _SEQUENCE_FORMATS[codec]

        # R16：目录目标语义。output_path 指向已存在目录 → 帧写进该目录
        # 内部（基名取 custom_name 或 sequence）；指向文件式基路径
        # （可带 .png/.tif 扩展名）→ 帧写在其父目录下以该基名命名。
        base = str(output_path or "sequence")
        if os.path.isdir(base):
            directory = base
            raw_base = str(getattr(config, "custom_name", "") or "") or "sequence"
        else:
            directory = os.path.dirname(base) or "."
            raw_base = os.path.splitext(os.path.basename(base))[0]
        self._directory = directory
        self._base = _sanitize_base(raw_base)

        # R17：显式作业字段优先；兼容 naming_config 回退（Quick Export
        # 与历史路径）。
        self._digits = int(getattr(config, "image_sequence_digits", 0) or 0) \
            or int(getattr(getattr(config, "naming_config", None),
                           "unique_filename_digits", 0) or 0) or 6
        explicit_start = getattr(config, "image_sequence_start_index", None)
        naming_start = getattr(
            getattr(config, "naming_config", None), "frame_number_start", None)
        fallback_start = explicit_start if explicit_start is not None else (
            naming_start if naming_start is not None
            else getattr(config, "start_frame", 0) or 0)
        self._start_index = int(fallback_start or 0)
        self._overwrite = bool(
            getattr(config, "image_sequence_overwrite", False))
        configured_depth = getattr(config, "bit_depth", None)
        if configured_depth is None:
            configured_depth = 16 if (
                self._codec_name == "exr" or getattr(config, "pix_fmt", "") == "rgb48le"
            ) else 8
        self._bit_depth = int(configured_depth)
        if icc_profile is not None and (self._codec_name != "tiff" or self._bit_depth != 16):
            raise ValueError("ICC-managed video delivery requires a 16-bit RGB TIFF sequence")
        self._icc_profile = icc_profile
        self._exr_space = exr_space or getattr(config, "exr_space", None)
        if self._codec_name == "exr":
            if self._exr_space not in ACES_EXR_CHROMATICITIES:
                raise ValueError("OpenEXR sequence delivery requires a linear ACES output space")
            if self._bit_depth not in (16, 32):
                raise ValueError("OpenEXR sequence delivery requires 16-bit half or 32-bit float")

        self._opened = False
        self._staging: Optional[str] = None
        self._committed: List[str] = []
        self._written: List[str] = []  # 暂存中的帧（abort 只清这些）
        self._frame_count = 0
        self._aborted = False
        self._codec_ctx = None
        self._frame_pattern = re.compile(
            r"^" + re.escape(self._base) + r"_(\d+)" + re.escape(self._ext) + r"$")

    # ── 生命周期 ──

    def open(self) -> None:
        os.makedirs(self._directory, exist_ok=True)
        # R15：同名冲突预检——目标目录里已存在本序列基名的帧文件时，
        # 默认失败（保护旧文件），显式授权覆盖才继续。
        conflicts = self._existing_frame_files()
        if conflicts and not self._overwrite:
            preview = ", ".join(os.path.basename(p) for p in conflicts[:5])
            raise ValueError(
                f"image sequence target already contains "
                f"{len(conflicts)} frame file(s) of '{self._base}' "
                f"(e.g. {preview}); enable overwrite to replace them")
        # 独立暂存目录：全部帧写完后 close 才提交（R15——取消/失败绝不
        # 触碰目标目录里的旧文件）。
        self._staging = os.path.join(
            self._directory, f".{self._base}{_STAGE_SUFFIX}")
        if os.path.isdir(self._staging):
            shutil.rmtree(self._staging, ignore_errors=True)
        os.makedirs(self._staging, exist_ok=True)
        self._opened = True
        logger.info(
            "Image sequence export: %s/%s_%0*d%s (bit_depth=%d%s)",
            self._directory, self._base, self._digits, self._start_index,
            self._ext, self._bit_depth,
            ", overwrite" if conflicts else "",
        )

    def close(self) -> None:
        """成功收尾：暂存帧原子提交到目标目录；授权覆盖时清理旧尾帧。"""
        if self._staging is None:
            logger.info("Image sequence export finished: %d frames",
                        self._frame_count)
            return
        last_index = self._start_index + self._frame_count - 1
        for path in self._written:
            target = os.path.join(self._directory, os.path.basename(path))
            os.replace(path, target)
            self._committed.append(target)
        shutil.rmtree(self._staging, ignore_errors=True)
        self._staging = None
        removed_stale = 0
        if self._overwrite and self._frame_count > 0:
            # 长→短重导出：同基名、帧号大于本次末帧的旧尾帧属于旧序列
            # 残留，授权覆盖语义下一并清理，避免混帧（R15）。
            for name in os.listdir(self._directory):
                m = self._frame_pattern.match(name)
                if m and int(m.group(1)) > last_index:
                    try:
                        os.remove(os.path.join(self._directory, name))
                        removed_stale += 1
                    except OSError:
                        logger.debug("stale tail remove failed: %s", name)
        self._written.clear()
        logger.info(
            "Image sequence export finished: %d frames committed, "
            "%d stale tail frames removed", self._frame_count, removed_stale)

    def abort(self) -> None:
        """取消：只删除本次暂存的帧——目标目录里的旧文件（即使同名）
        保持原样，失败/取消后旧序列哈希不变（R15）。"""
        self._aborted = True
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)
            self._staging = None
        self._written.clear()
        logger.info("Image sequence export aborted; staged frames discarded")

    # ── 诊断 / manifest（R16） ──

    def sequence_info(self) -> Dict[str, Any]:
        """本次导出的序列清单（帧文件/首尾帧号/目录/位深）。"""
        return {
            "directory": self._directory,
            "base": self._base,
            "frame_files": list(self._committed),
            "first_frame": self._start_index if self._frame_count else None,
            "last_frame": (
                self._start_index + self._frame_count - 1
                if self._frame_count else None),
            "digits": self._digits,
            "bit_depth": self._bit_depth,
            "codec": self._codec_name,
        }

    def _existing_frame_files(self) -> List[str]:
        """目标目录中与序列命名冲突的既有帧文件。"""
        if not os.path.isdir(self._directory):
            return []
        return sorted(
            os.path.join(self._directory, name)
            for name in os.listdir(self._directory)
            if self._frame_pattern.match(name)
        )

    # ── 视频帧 ──

    def encode_frame(self, decoded_frame: Any) -> None:
        """写一帧：BGR(HxWxC, uint8/uint16) → RGB 图像包 → 暂存文件。"""
        if not self._opened:
            self.open()
        if self._aborted:
            raise RuntimeError("image sequence encoder was aborted")

        import numpy as np
        data = getattr(decoded_frame, "data", None)
        if data is None:
            raise TypeError("image sequence requires packed frame data")
        if data.ndim != 3:
            raise ValueError(f"expected HxWxC frame, got shape {data.shape}")
        channels = data.shape[-1]
        if channels not in (3, 4):
            raise ValueError(f"expected 3/4 channels, got {channels}")

        frame_index = self._start_index + self._frame_count
        filename = f"{self._base}_{frame_index:0{self._digits}d}{self._ext}"
        assert self._staging is not None
        path = os.path.join(self._staging, filename)
        if self._codec_name == "exr":
            self._write_exr_frame(path, data)
        elif self._icc_profile is not None:
            self._write_profiled_tiff(path, data)
        else:
            self._write_unprofiled_frame(path, data)
        self._written.append(path)
        self._frame_count += 1

    def _write_profiled_tiff(self, path: str, data: Any) -> None:
        """Write unchanged 16-bit RGB(A) samples with their ICC profile."""
        import numpy as np
        from PyQt6.QtGui import QColorSpace, QImage

        if data.dtype != np.uint16:
            raise TypeError(f"ICC TIFF requires uint16 samples, got {data.dtype}")
        height, width, channels = data.shape
        rgba = np.empty((height, width, 4), dtype=np.uint16)
        rgba[..., :3] = data[..., 2::-1]
        rgba[..., 3] = data[..., 3] if channels == 4 else 65535
        image_format = QImage.Format.Format_RGBA64 if channels == 4 else QImage.Format.Format_RGBX64
        image = QImage(rgba.data, width, height, rgba.strides[0], image_format)
        space = QColorSpace.fromIccProfile(self._icc_profile)
        if not space.isValid():
            raise ValueError("Invalid TIFF delivery ICC profile")
        image.setColorSpace(space)
        if not image.save(path, "TIFF"):
            raise OSError(f"Could not write ICC TIFF frame {path}")

    def _write_exr_frame(self, path: str, data: Any) -> None:
        """Write linear BGR(A) with ACES chromaticities and associated alpha."""
        import numpy as np
        import OpenEXR

        if data.dtype.kind != "f":
            raise TypeError(f"OpenEXR requires floating-point samples, got {data.dtype}")
        dtype = np.float16 if self._bit_depth == 16 else np.float32
        rgb = data[..., :3][..., ::-1]
        alpha = None
        if bool(getattr(self._config, "has_alpha", data.shape[-1] == 4)):
            alpha = data[..., 3] if data.shape[-1] == 4 else np.ones(data.shape[:2], data.dtype)
            if not getattr(self._config, "alpha_premultiplied", False):
                rgb = rgb * alpha[..., None]
        if self._bit_depth == 16 and np.any(np.abs(rgb) > np.finfo(np.float16).max):
            raise ValueError("OpenEXR half-float RGB overflow. Choose 32-bit float output.")
        channels = {name: np.ascontiguousarray(rgb[..., i], dtype=dtype)
                    for i, name in enumerate("RGB")}
        if alpha is not None:
            channels["A"] = np.ascontiguousarray(alpha, dtype=dtype)
        header = {
            "type": OpenEXR.scanlineimage,
            # PIZ preserves float samples and avoids the platform-dependent
            # native ZIP encoder path used by some OpenEXR wheels.
            "compression": OpenEXR.PIZ_COMPRESSION,
            "chromaticities": ACES_EXR_CHROMATICITIES[self._exr_space],
        }
        OpenEXR.File(header, channels).write(path)

    def _write_unprofiled_frame(self, path: str, data: Any) -> None:
        import av
        import numpy as np

        height, width, channels = data.shape
        # BGR → RGB（源渲染帧约定为 BGR 字节序）
        if channels == 4:
            rgb = data[:, :, [2, 1, 0, 3]]
        else:
            rgb = data[:, :, [2, 1, 0]]
        rgb = np.ascontiguousarray(rgb)

        # 像素格式映射（R18）：PNG 16-bit 标准为大端格式名（rgb48be/
        # rgba64be——ffmpeg png 编码器无 le 变体），但 from_ndarray 对 be
        # 格式自行处理字节序——传原生死序数组即可，手动 byteswap 会
        # 二次交换损坏数值（[30,250,100,200] → ×256）。TIFF 用 le。
        if rgb.dtype == np.uint8:
            fmt = "rgba" if channels == 4 else "rgb24"
            frame_array = rgb
        elif rgb.dtype == np.uint16:
            if self._codec_name == "png":
                fmt = "rgba64be" if channels == 4 else "rgb48be"
            else:
                fmt = "rgba64le" if channels == 4 else "rgb48le"
            frame_array = rgb
        else:
            raise TypeError(f"unsupported frame dtype: {rgb.dtype}")

        frame = av.VideoFrame.from_ndarray(frame_array, format=fmt)
        ctx = self._get_codec_context(fmt, width, height)
        packets = list(ctx.encode(frame))
        if not packets:
            raise RuntimeError(f"{self._codec_name} encode produced no packet")
        with open(path, "wb") as fh:
            for packet in packets:
                fh.write(bytes(packet))

    def _get_codec_context(self, pix_fmt: str, width: int, height: int):
        """按像素格式惰性创建/复用编码上下文（无损默认参数）。"""
        from fractions import Fraction

        import av

        if self._codec_ctx is not None:
            return self._codec_ctx
        ctx = av.CodecContext.create(self._codec_name, "w")
        # avcodec_open2 需要 width/height/pix_fmt（部分构建还要 time_base）
        ctx.width = int(width)
        ctx.height = int(height)
        ctx.pix_fmt = pix_fmt
        try:
            ctx.time_base = Fraction(1, 24)
        except Exception:  # noqa: BLE001 — 个别构建只读
            pass
        self._codec_ctx = ctx
        return ctx

    # ── 音频（图像序列不承载） ──

    def write_audio_samples(self, samples, sample_rate: int = 48000) -> None:  # noqa: ANN001
        import numpy as np  # noqa: F401 — 签名兼容

        logger.warning(
            "image sequence export ignores audio (sequence has no audio track)")


def image_sequence_stats(output_path: str) -> Optional[dict]:
    """统计目录式序列输出（帧数/首尾帧号），供验收与诊断。"""
    target = str(output_path)
    directory = target if os.path.isdir(target) else (
        os.path.dirname(target) or ".")
    if not os.path.isdir(directory):
        return None
    exts = (".png", ".tif", ".tiff", ".exr")
    files = sorted(
        f for f in os.listdir(directory)
        if f.lower().endswith(exts) and "_" in f
    )
    if not files:
        return None
    numbers = []
    for name in files:
        stem = os.path.splitext(name)[0]
        tail = stem.rsplit("_", 1)[-1]
        if tail.isdigit():
            numbers.append(int(tail))
    if not numbers:
        return None
    return {
        "directory": directory,
        "frame_files": len(numbers),
        "first_frame": min(numbers),
        "last_frame": max(numbers),
    }
