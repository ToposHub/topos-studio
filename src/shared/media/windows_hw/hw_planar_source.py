"""Windows 硬件解码 planar 源（NVDEC/D3D11VA → NV12/P010 紧凑平面下载）。

与 ``MacOSVtPlanarSource`` 产出完全同契约的 ``DecodedFrame``：

- ``data=None``，``planes=(Y, UV)`` 恰好两个紧凑平面
- ``Y``: ``(H, W)``；``UV``: ``(H//2, W)`` 2D 交错（U/V 交错，非 3D）
- ``pixel_format``: ``'nv12'``（8bit）/ ``'p010'``（10bit，值域 0..1023 低对齐）
- 原生分辨率解码，不做 CPU 缩放（缩放由渲染器 GPU pass 完成）

渲染链路（``_upload_and_render_planar`` → ``_classify_planar_format`` →
``upload_nv12``/``upload_p010``）按 ``pixel_format`` 自动路由，无需改动。

实现说明：
- PyAV 17+/18 的 ``HWAccel`` API 会自动把硬解帧下载为软件帧
  （直接拿到 nv12/p010le），无需手动 ``frame.transfer()``。
- P010 的 10bit 值存储在 uint16 高 10 位（MSB 对齐，实测值域
  0..65520）。按 ``p016le``/16bit 契约原样上传（GPU 按 65535 归一化，
  与理论的 65472 差 0.096%，低于 10bit 量化精度），避免每帧 ~5ms 的
  CPU 移位遍历 —— 见 ``_HW_SEMI_PLANAR_FORMATS`` 注释。
- 流无法硬解时（如 H.264 4:2:2 10-bit，NVDEC/D3D11 均不支持），
  PyAV 会话可能成功打开但仍输出软件格式（yuv422p10le 等）。
  ``open()`` 会解码首帧验证输出格式属于 nv12/p010le/p016le 才启用，
  否则抛 ``MediaOpenError``，由调用方回退 CPU planar 源。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np

from src.shared.media.color_metadata import (
    map_color_range,
    normalize_color_primaries,
    normalize_color_space,
    normalize_color_transfer,
)
from src.shared.media.ffmpeg_source import FFmpegMediaSource
from src.shared.media.frame import DecodedFrame, PlaneInfo
from src.shared.media.frame_path import DecodePath
from src.shared.media.hwaccel import HardwareCapability, HardwareDecoder
from src.shared.media.source import MediaOpenError

from .reasons import codec_unsupported_reason

logger = logging.getLogger(__name__)

# 硬解会话实际产出（PyAV 自动下载后）的半平面格式 → 转换规格
# (输出契约格式, dtype, bit_depth)。
#
# P010 的 10bit 值存于 uint16 高 10 位（MSB 对齐，实测值域 0..65520）。
# 不做 CPU 端 >>6 对齐（4K Y+UV 每帧 ~5ms 的纯内存遍历，恰好是本源
# 要卸载掉的 CPU 负载），而是按 p016le/16bit 契约原样上传：GPU shader
# 以 65535 归一化，与理论的 65472（=1023<<6）相差 0.096% —— 低于
# 10bit 量化精度（1/1023），视觉不可辨。
_HW_SEMI_PLANAR_FORMATS: dict[str, tuple[str, Any, int]] = {
    'nv12': ('nv12', np.dtype(np.uint8), 8),
    'p010le': ('p016le', np.dtype(np.uint16), 16),
    'p016le': ('p016le', np.dtype(np.uint16), 16),
}

# HardwareDecoder 枚举 → PyAV HWDeviceType 名称
_DEVICE_TYPE_NAMES: dict[str, str] = {
    'nvdec': 'cuda',
    'cuda': 'cuda',
    'd3d11va': 'd3d11va',
    'qsv': 'qsv',
}

# AUD-36200：归一化后视为"流未标记"的色彩值（FFmpeg unspecified=2 /
# range=0 / colorspace=3(reserved) 经 normalize/map 后的产物）。
# 这些值不得写入帧色彩标签——让回退链继续找真实元数据或渲染缺省。
_UNSPECIFIED_COLOR_TAGS: frozenset = frozenset(
    {'', 'unknown', 'unspecified', 'reserved'}
)

# DecodedFrame 色彩字段 → (解码帧属性名, 归一化函数)。
# PyAV 17 的 VideoStream/CodecContext 不暴露任何 color_* 属性（实测
# dir() 均为空），av.VideoFrame 是码流色彩标记（SPS/VUI）唯一的
# Python 侧来源，返回 FFmpeg 整型枚举值——用与父类容器探测
# （ffmpeg_source._probe_metadata）一致的 normalize/map 函数归一化。
_FRAME_COLOR_READERS: dict[str, tuple[str, Any]] = {
    'color_primaries': ('color_primaries', normalize_color_primaries),
    'color_transfer': ('color_trc', normalize_color_transfer),
    'color_matrix': ('colorspace', normalize_color_space),
    'color_range': ('color_range', map_color_range),
}

# 渲染器侧缺省（edit_gpu_preview_renderer._decoded_frame_color_tag 的
# default 参数同为 bt709/limited）——仅在流与探测元数据都未标记时使用。
_DEFAULT_COLOR_TAGS: dict[str, str] = {
    'color_primaries': 'bt709',
    'color_transfer': 'bt709',
    'color_matrix': 'bt709',
    'color_range': 'limited',
}


def _is_unspecified_color_tag(value: Any) -> bool:
    """归一化后的色彩值是否表示"流未标记"（None/空/unknown 族）。"""
    if value is None:
        return True
    return str(value).strip().lower() in _UNSPECIFIED_COLOR_TAGS


class WindowsHwPlanarSource(FFmpegMediaSource):
    """Windows 硬件解码 + 半平面 YUV 下载的 planar 媒体源。

    仅适用于 H.264/H.265 的 4:2:0 8/10/16bit 流（NV12/P010/P016）。
    open() 验证失败时抛出 ``MediaOpenError``，调用方应回退到
    ``FFmpegMediaSource``（CPU planar）。
    """

    def __init__(
        self,
        path: str,
        decoder: Optional[HardwareDecoder] = None,
    ) -> None:
        super().__init__(path, preserve_planes=True, output_mode='planar_yuv')
        self._hw_decoder: Optional[HardwareDecoder] = decoder
        self._hw_active: bool = False
        self._hw_output_format: Optional[str] = None
        # AUD-36200：容器探测色彩元数据（open() 末尾刷新），逐帧色彩
        # 标签的回退值；未标记字段保持渲染器缺省 bt709/limited。
        self._probe_color_tags: dict[str, str] = dict(_DEFAULT_COLOR_TAGS)

    # ------------------------------------------------------------------
    # 打开 / 硬解会话
    # ------------------------------------------------------------------

    def open(self) -> None:
        """打开媒体并启用硬件解码；验证失败抛 ``MediaOpenError``。

        AUD-36201：失败路径必须关闭残留容器并复位 ``_is_open``——
        消费方按"open 抛错 = 源不可用、无需 close"的契约回退 CPU
        planar 源，半开状态的 hwaccel 容器句柄会随实例一起泄漏。
        """
        super().open()  # CPU 容器打开 + 元数据探测
        try:
            self._open_with_hw_session()
        except MediaOpenError:
            self.close()  # 复用父类完整复位路径（容器/迭代器/_is_open）
            raise

    def _open_with_hw_session(self) -> None:
        """open() 的硬解会话装配（在 super().open() 成功后调用）。"""
        if self._metadata is None:
            raise MediaOpenError("Metadata probe failed", source_path=self._path)

        codec = self._metadata.codec_name or ''
        decoder = self._hw_decoder or HardwareCapability.get_best_decoder_for_codec(codec)
        if decoder is None or not HardwareCapability.check_codec_support(decoder, codec):
            raise MediaOpenError(
                f"{codec_unsupported_reason(codec)}: No hardware decoder for codec={codec}",
                source_path=self._path,
            )

        self._reopen_with_hwaccel(decoder)
        self._verify_hw_output()
        self._probe_color_tags = self._resolve_probe_color_tags()

        self._is_planar_mode = True
        self._hw_active = True
        logger.info(
            "WindowsHwPlanarSource: %s hw decode via %s -> %s (%s)",
            self._path, decoder.value, self._hw_output_format, codec,
        )

    def _reopen_with_hwaccel(self, decoder: HardwareDecoder) -> None:
        """关闭 CPU 容器，用 PyAV HWAccel API 重新打开（复用 hwaccel.py 的模式）。"""
        try:
            from av.codec.hwaccel import HWAccel, HWDeviceType
        except ImportError as e:
            raise MediaOpenError(
                "PyAV lacks the HWAccel API (need PyAV 14+)",
                source_path=self._path,
            ) from e

        device_name = _DEVICE_TYPE_NAMES.get(decoder.value)
        if device_name is None:
            raise MediaOpenError(
                f"Unsupported Windows hw decoder: {decoder.value}",
                source_path=self._path,
            )
        accel = HWAccel(getattr(HWDeviceType, device_name))

        import av

        self._frame_iterator = None
        self._decoder_buffer = []
        self._pending_av_frame = None
        if self._container is not None:
            try:
                self._container.close()
            except Exception:
                pass
            self._container = None
            self._video_stream = None

        try:
            self._container = av.open(self._path, hwaccel=accel)
        except Exception as e:
            raise MediaOpenError(
                f"hwaccel open failed ({decoder.value}): {e}",
                source_path=self._path,
            ) from e

        video_streams = self._container.streams.video
        if not video_streams:
            raise MediaOpenError(
                "No video stream on hwaccel reopen", source_path=self._path
            )
        self._video_stream = video_streams[0]
        # 注意：不要设置 thread_type='AUTO'——帧线程与 hwaccel 互斥，
        # FFmpeg 会静默禁用硬解回退软件解码（H.264 实测首帧变 yuv420p）
        self._is_open = True
        self._hw_decoder = decoder

    def _verify_hw_output(self) -> None:
        """解码首帧验证硬解输出格式；帧缓存到 _pending_av_frame 不浪费解码。

        PyAV 在流无法硬解时可能静默回退软件解码（输出 yuv422p10le 等），
        此处按实际输出格式门控：非半平面格式视为硬解不适用。
        """
        assert self._container is not None and self._video_stream is not None
        try:
            first = next(self._container.decode(self._video_stream))
        except StopIteration as e:
            raise MediaOpenError("No decodable frames", source_path=self._path) from e
        except Exception as e:
            raise MediaOpenError(
                f"hw decode probe failed: {e}", source_path=self._path
            ) from e

        fmt = first.format.name if first.format else 'unknown'
        if fmt not in _HW_SEMI_PLANAR_FORMATS:
            metadata = getattr(self, '_metadata', None)
            codec_name = (metadata.codec_name if metadata else '') or ''
            raise MediaOpenError(
                f"{codec_unsupported_reason(codec_name, fmt)}: "
                f"hardware decoder produced {fmt} (not NV12/P010-family); "
                f"stream is not hardware-decodable",
                source_path=self._path,
            )
        self._hw_output_format = fmt
        # 首帧交还给顺序读取路径消费，帧号从 0 起算
        self._pending_av_frame = first
        self._current_frame = 0

    # ------------------------------------------------------------------
    # 色彩元数据（AUD-36200）
    # ------------------------------------------------------------------

    def _resolve_probe_color_tags(self) -> dict[str, str]:
        """容器探测色彩元数据 → 逐字段回退渲染器缺省（bt709/limited）。

        来源是父类 ``open()`` 填充的 ``self._metadata``（probe 阶段读
        stream/codec/codec_context）。注意 PyAV 17 的流对象不暴露
        color_* 属性，探测值常为 None——真实的码流标记（HDR10 的
        bt2020nc/smpte2084、full-range）由解码帧携带，在
        ``_frame_color_tag`` 中逐帧优先读取，本方法只提供回退值。
        """
        tags: dict[str, str] = {}
        metadata = self._metadata
        for field, default in _DEFAULT_COLOR_TAGS.items():
            value = getattr(metadata, field, None) if metadata is not None else None
            if _is_unspecified_color_tag(value):
                tags[field] = default
            else:
                tags[field] = str(value).strip().lower()
        return tags

    def _frame_color_tag(self, frame: Any, field: str) -> str:
        """读取解码帧携带的码流色彩标记；未标记时走回退链。

        回退链（与渲染器 ``_decoded_frame_color_tag`` 的优先级语义
        对齐，但把"屏蔽元数据的硬编码"换成真实值）：
        1. 解码帧 color_*（码流 SPS/VUI 标记，HDR10/full-range 权威来源）
        2. 容器探测值（``_probe_color_tags``，open() 时解析）
        3. 渲染器缺省 bt709/limited（``_DEFAULT_COLOR_TAGS``）
        """
        fallback = (
            getattr(self, '_probe_color_tags', {}).get(field)
            or _DEFAULT_COLOR_TAGS[field]
        )
        attr_name, normalize = _FRAME_COLOR_READERS[field]
        raw = getattr(frame, attr_name, None)
        if raw is None:
            return fallback
        normalized = normalize(raw)
        if _is_unspecified_color_tag(normalized):
            return fallback
        return str(normalized).strip().lower()

    # ------------------------------------------------------------------
    # planar 帧转换（覆盖 FFmpegMediaSource 的唯一转换点）
    # ------------------------------------------------------------------

    def _convert_frame_planar(self, frame: Any) -> DecodedFrame:
        """硬解半平面帧 → 契约 DecodedFrame；其他格式走父类通用路径。

        P010 输出为 p016le/16bit 契约（值保持 MSB 对齐原样上传，
        见 _HW_SEMI_PLANAR_FORMATS 注释）；NV12 输出 nv12 契约。
        """
        frame = self._transfer_hw_frame_if_needed(frame)
        fmt = frame.format.name if getattr(frame, 'format', None) else 'unknown'

        spec = _HW_SEMI_PLANAR_FORMATS.get(fmt)
        if spec is None:
            # 非预期的软件格式（硬解中途失效等）→ 父类通用 planar 提取兜底
            return super()._convert_frame_planar(frame)

        contract_format, dtype, bit_depth = spec
        width, height = frame.width, frame.height

        y_plane = self._extract_visible_plane_data(
            av_plane=frame.planes[0],
            plane_width=width,
            plane_height=height,
            dtype=dtype,
            pix_fmt=fmt,
        )
        uv_plane = self._extract_visible_plane_data(
            av_plane=frame.planes[1],
            plane_width=width,   # UV 交错：每行 w 个样本（w/2 U + w/2 V）
            plane_height=height // 2,
            dtype=dtype,
            pix_fmt=fmt,
        )

        pts = frame.pts
        time_seconds = 0.0
        if pts is not None and self._video_stream is not None:
            if self._video_stream.time_base is not None:
                time_seconds = float(pts * self._video_stream.time_base)

        itemsize = dtype.itemsize
        decoded = DecodedFrame(
            frame_index=self._current_frame,
            pts=pts,
            time_seconds=time_seconds,
            data=None,
            planes=(y_plane, uv_plane),
            pixel_format=contract_format,
            dtype=dtype,
            width=width,
            height=height,
            metadata=self._metadata,
            is_proxy=False,
            plane_infos=(
                PlaneInfo.create(
                    index=0, width=width, height=height, component='Y',
                    dtype=dtype, bit_depth=bit_depth,
                ),
                PlaneInfo.create(
                    index=1, width=width // 2, height=height // 2, component='UV',
                    dtype=dtype, bit_depth=bit_depth,
                    stride=width * itemsize,
                ),
            ),
            extra={
                'decode_path': DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV.value,
                'source': 'windows_hw_planar',
                'hw_decoder': self._hw_decoder.value if self._hw_decoder else None,
                'hw_output_format': fmt,
                # p016le 契约携带的是 p010 MSB 对齐码值（未 >>6）
                'msb_aligned_p010': fmt == 'p010le',
            },
        )
        # AUD-36200：色彩标签来自流真实元数据（解码帧码流标记 →
        # 容器探测 → bt709/limited 渲染缺省），绝不硬编码覆盖——
        # 否则 BT.2020/PQ（HDR10，P010 路径正是为此保留）与
        # full-range 素材会被按 BT.709/limited 错误解码。
        decoded.color_range = self._frame_color_tag(frame, 'color_range')
        decoded.color_matrix = self._frame_color_tag(frame, 'color_matrix')
        decoded.color_primaries = self._frame_color_tag(frame, 'color_primaries')
        decoded.color_transfer = self._frame_color_tag(frame, 'color_transfer')
        return decoded

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def using_hwaccel(self) -> bool:
        """硬解会话是否成功启用。"""
        return self._hw_active

    @property
    def active_decoder(self) -> Optional[HardwareDecoder]:
        """实际使用的硬件解码器。"""
        return self._hw_decoder

    @property
    def hw_output_format(self) -> Optional[str]:
        """硬解下载后的像素格式（nv12/p010le/p016le）。"""
        return self._hw_output_format
