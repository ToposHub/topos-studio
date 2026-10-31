"""ToposImageSource —— .toos 静态图媒体源（Topos Image 阶段 5）。

实现 MediaSource 协议，把 .toos 单帧静态图接入现有媒体链：
- 解码输出 planar uint16 DecodedFrame（pixel_format 名复用视频链注册名：
  无 Alpha 用 'gbrp10le'/'yuv422p10le'…，带 Alpha 用 'topos_*a{N}'——
  has_alpha/planes 元数据与实际 4 平面一致，GPU 上传链零新增接线）；
- 通道/平面序沿用 codec 契约：GBR=G,B,R、YUV=Y,U,V、A 恒 plane 3
  （spec §6.1，ADR-C017 C-130）；alpha 平面按声明位深满刻度交付
  （spec §8.6，与视频链同源）；
- 静态图语义：total_frames=1，read_frame 仅接受 0；
- 色彩/Alpha 元数据来自 IDSC，extra 键名对齐视频链（topos_*），与导出、
  缩略图、颜色管理同源（不静默转换）。

边界（.clinerules）：
- 本类只做解码与元数据搬运，不做 OpenGL/渲染；UI 通过既有媒体链消费；
- 失败/关闭路径显式释放资源；native 错误抛 ToposImageError（有诊断）。
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from ..codec.topos_image_binding import (
    TC_IMG_CHUNK_ICCP,
    TC_IMG_CHUNK_OCIO,
    TC_IMG_SAMPLE_KIND_HALF,
    ToposImageCodec,
    ToposImageError,
    get_image_codec,
)
from ..codec.topos_meta import topos_image_tier_label
from .color_decode import ToposColorMetadataError, resolve_topos_color_semantics
from .color_metadata import (
    map_color_primaries,
    map_color_space,
    map_color_transfer,
)
from .frame import DecodedFrame, PlaneInfo
from .metadata import Rational, VideoMetadata
from .source import MediaOpenError, MediaSourceError

logger = logging.getLogger(__name__)

# pixel_format 注册名（与 topos_source._PIXEL_FORMAT_NO_ALPHA 同源语义）：
# 无 Alpha 复用基础色度名（GPU 上传链零新增接线）；带 Alpha 使用与视频链
# 相同的 topos_*a{N} 注册名（frame.py PIXEL_FORMATS），使 format_info.has_alpha
# /planes 与实际 4 平面一致——否则次要消费路径按 3 平面名静默压平 Alpha。
# (2,16) 浮点（v1.8 ADR-C057 / M1-F1）：sample_kind=HALF 的 GBR float16
# 线性平面（3 平面，G,B,R 序；仅 sample_kind=HALF 时走该名——16-bit 整数
# GBR 文件仍不在此域）。HALF+Alpha 组合不入表（编码器组合冻结 no-alpha，
# 读端显式拒绝）。
_PIXEL_FORMAT_NO_ALPHA = {
    (0, 10): 'yuv422p10le', (0, 12): 'yuv422p12le',
    (1, 10): 'yuv444p10le', (1, 12): 'yuv444p12le',
    (2, 10): 'gbrp10le', (2, 12): 'gbrp12le',
    (2, 16): 'gbrph16le',   # HALF 浮点（float16 线性值平面）
    (3, 12): 'topos_cfa12le',  # TRAW（v1.6 批 1）：CFA 4 相位平面
    (3, 16): 'topos_cfa16le',  # TRAW（批 4）：16-bit linear 归档档
}


def _pixel_format_name(pixel_format: int, bit_depth: int,
                       alpha_bit_depth: int) -> str:
    """pixel_format 注册名；未知组合显式 KeyError（M1-F1：不再静默回退
    'yuv422p10le' 错标——错标比 open 失败更糟，调用方必须看到明确诊断）。

    AUD-35813：带 Alpha 时预留/未知 pixel_format（>=4）同样显式
    KeyError——旧实现落入 ``'444' if pf == 1 else '422'`` 兜底分支，
    被误标为 YUV 422 名。"""
    if alpha_bit_depth <= 0:
        return _PIXEL_FORMAT_NO_ALPHA[(pixel_format, bit_depth)]
    if pixel_format == 2:
        return f'topos_gbrap{bit_depth}a{alpha_bit_depth}'
    if pixel_format in (0, 1):
        cf = '444' if pixel_format == 1 else '422'
        return f'topos_yuva{cf}p{bit_depth}a{alpha_bit_depth}'
    # CFA(3) 与 alpha 互斥；>=4 为预留/未知值——都不允许拼出注册名
    raise KeyError(
        f"unsupported .toos pixel_format with alpha: {pixel_format}")

# 平面分量名（spec §6.1 通道顺序；TRAW 批 1 增 CFA 相位序）
_COMPONENTS = {
    0: ('Y', 'U', 'V', 'A'),
    1: ('Y', 'U', 'V', 'A'),
    2: ('G', 'B', 'R', 'A'),
    3: ('R', 'Gr', 'Gb', 'B'),
}


class ToposImageSource:
    """单帧 .toos 静态图源（MediaSource 协议的最小实现）。"""

    def __init__(self, path: str, codec: Optional[ToposImageCodec] = None) -> None:
        self._path = str(path)
        self._codec = codec
        self._open = False
        self._current_frame = 0
        self._info = None
        self._metadata: Optional[VideoMetadata] = None
        self._color_semantics = None

    # —— MediaSource 协议 ——

    def open(self) -> None:
        if self._open:
            return
        codec = self._codec or get_image_codec()
        try:
            # probe 而非 validate：validate 会对含 PIXL 在内的全部 chunk 流式
            # CRC（≈整文件读一遍），decode 内部还会再 probe+整读 PIXL——
            # open 阶段整文件 CRC 属重复 I/O（阶段 7 实测逆风项）。
            # 完整性校验交给显式 verify/deep 调用方。
            self._info = codec.probe(self._path)
        except FileNotFoundError as exc:
            raise MediaOpenError(f"文件不存在: {self._path}", self._path) from exc
        except PermissionError as exc:
            raise MediaOpenError(f"无权限读取: {self._path}", self._path) from exc
        except OSError as exc:
            # IsADirectoryError 等 IO 层错误统一映射为工厂可诊断的 MediaOpenError
            raise MediaOpenError(f".toos 打开失败: {exc}", self._path) from exc
        except ToposImageError as exc:
            raise MediaOpenError(f".toos 探测失败: {exc}", self._path) from exc
        assert self._info is not None
        try:
            icc_profile = (codec.read_metadata(self._path, TC_IMG_CHUNK_ICCP)
                           if self._info.iccp_ref else None)
            ocio_name = None
            if self._info.ocio_ref:
                ocio_name = codec.read_metadata(self._path, TC_IMG_CHUNK_OCIO).rstrip(b"\x00").decode("utf-8")
            self._color_semantics = resolve_topos_color_semantics({
                'pixel_format': self._info.pixel_format,
                'bit_depth': self._info.bit_depth,
                'color_range': self._info.color_range,
                'color_primaries': self._info.color_primaries,
                'color_transfer': self._info.color_transfer,
                'color_matrix': self._info.color_matrix,
            }, icc_profile=icc_profile, ocio_colorspace=ocio_name)
        except (ToposColorMetadataError, UnicodeDecodeError) as exc:
            raise MediaOpenError(f".toos 色彩元数据冲突: {exc}", self._path) from exc
        alpha_bd = self._info.alpha_bit_depth if self._info.has_alpha else 0
        unsupported = (
            alpha_bd <= 0
            and (self._info.pixel_format, self._info.bit_depth)
            not in _PIXEL_FORMAT_NO_ALPHA
        ) or (alpha_bd > 0 and (
            self._info.bit_depth not in (10, 12)
            # CFA 与 alpha 互斥（native 编码域契约；纵深防御手拼流）；
            # 预留/未知 pixel_format（>=4）叠 Alpha 同样不支持——旧校验
            # 漏放行后被 _pixel_format_name 误标为 YUV 422 名（AUD-35813）
            or self._info.pixel_format not in (0, 1, 2)
        ))
        # (2,16) 仅 HALF 样本域（浮点档，sample_kind 自描述）；UINT 16-bit
        # GBR 无产品档——错挂浮点名比显式拒绝更糟（M1-F1/F7 钉扎）。
        if (not unsupported and (self._info.pixel_format, self._info.bit_depth) == (2, 16)
                and int(self._info.sample_kind) != TC_IMG_SAMPLE_KIND_HALF):
            unsupported = True
        if unsupported:
            raise MediaOpenError(
                f"不支持的 .toos 组合 pixel_format={self._info.pixel_format} "
                f"bit_depth={self._info.bit_depth}", self._path)
        pf_name = _pixel_format_name(
            self._info.pixel_format, self._info.bit_depth, alpha_bd)
        # v1.7 TMET 容器元数据：档位/厂商（缺失/损坏静默降级——只服务展示）。
        # 优先级：TMET label（导出侧写死的展示名，如 'toos High 12bit' 或
        # 视频档位 'Topos 422'）→ 图片 tier id + 位深（+ 样本域）组合 →
        # image_profile 类名兜底（旧文件）。
        meta = codec.read_export_meta(self._path) or {}
        # v1.8（ADR-C057）：浮点样本域（HALF）展示名带 ' float' 后缀——
        # 与整数阶梯共用 tier id，域由 IDSC sample_kind 自描述。
        floating = (int(self._info.sample_kind) == TC_IMG_SAMPLE_KIND_HALF)
        profile_label = (meta.get('label') or ''
                         or topos_image_tier_label(
                             meta.get('tier_id') or 0, self._info.bit_depth,
                             floating)
                         or self._profile_name(self._info.image_profile,
                                               self._info.bit_depth))
        self._metadata = VideoMetadata(
            width=self._info.visible_width,
            height=self._info.visible_height,
            fps=Rational(1, 1),  # 声明类型为 Rational（Fraction 缺 .num/.den/.to_fps）
            total_frames=1,
            codec_name='topos-image',
            codec_profile=profile_label,
            pixel_format=pf_name,
            bit_depth=self._info.bit_depth,
            color_range='full' if self._info.color_range == 1 else 'limited',
            # IDSC 色彩码 → 标准字符串（M1-F5/G1 基础：此前恒 None 的断链
            # 修复——时间线 IDT 与导出侧按字符串消费）。20=TRAW_LOG0 随
            # RC7 词表统一映射 'traw-log0'（CFA 专用分支/导出 SDR 回退承接）。
            color_primaries=map_color_primaries(self._info.color_primaries),
            color_transfer=map_color_transfer(self._info.color_transfer),
            color_matrix=map_color_space(self._info.color_matrix),
            has_alpha=self._info.has_alpha,
            field_order='progressive',
            rotation=0,
            # IDSC pixel_aspect 进元数据（非方像素按 metadata 布局的消费方不再
            # 显示比例错误）；orientation 经 extra 透传（v1 恒为已规范化值 1）
            sample_aspect_ratio=Rational(*self._info.pixel_aspect)
            if self._info.pixel_aspect[1] > 0 else None,
        )
        self._open = True

    def close(self) -> None:
        self._open = False
        self._info = None
        self._metadata = None
        self._color_semantics = None

    def probe(self) -> VideoMetadata:
        if self._metadata is None:
            self.open()
        assert self._metadata is not None
        return self._metadata

    def read_frame(self, frame_index: int) -> DecodedFrame:
        if frame_index != 0:
            raise MediaSourceError(
                f".toos 为单帧静态图，frame_index 必须为 0（得到 {frame_index}）",
                self._path)
        if not self._open:
            self.open()
        self._current_frame = 1  # 游标：已读取（单帧源读后 = 1，seek 重置为 0）
        assert self._info is not None and self._metadata is not None
        assert self._color_semantics is not None
        codec = self._codec or get_image_codec()
        # M1-F2：HALF 样本域（v1.8 浮点档）走 decode_float——输出 float16
        # 线性值平面（G,B,R 序，值域含负值/>1 高光，无满刻度契约）；UINT
        # 文件仍走 decode()（样本域混用是 binding 层显式错误）。
        floating = int(self._info.sample_kind) == TC_IMG_SAMPLE_KIND_HALF
        if floating:
            planes, info, extra = codec.decode_float(self._path)
        else:
            planes, info, extra = codec.decode(self._path)
        assert info is not None
        w, h = info.visible_width, info.visible_height
        comps = _COMPONENTS.get(info.pixel_format, ('P0', 'P1', 'P2', 'A'))
        planes = list(planes)
        plane_dtype = np.float16 if floating else np.uint16
        if not floating:
            # spec §8.6（与视频链同源）：码流内 alpha 是 16-bit 满刻度容器，
            # alpha_bit_depth 只是顶层 N-bit 预量化精度；应用层平面语义统一为
            # "声明位深满刻度"（GPU 上传按 plane_infos[3].bit_depth 归一化）。
            # 浮点域无满刻度契约（线性 HDR 值域），alpha 不缩放（且组合冻结
            # no-alpha——open 已拒）。
            if info.has_alpha and 0 < info.alpha_bit_depth < 16:
                planes[3] = planes[3] >> (16 - info.alpha_bit_depth)
        # CFA（pf=3）= 4 相位平面全主位深（R/Gr/Gb/B）；其余 3 主平面
        # （2026-09-13 修复：此前恒 3 项，pf=3 时 bit_depths[3] 越界崩溃）
        bit_depths = [info.bit_depth] * (4 if info.pixel_format == 3 else 3)
        if info.has_alpha:
            bit_depths.append(info.alpha_bit_depth)
        plane_infos = tuple(
            PlaneInfo.create(index=i, width=p.shape[1], height=p.shape[0],
                             component=comps[i], dtype=plane_dtype,
                             bit_depth=bit_depths[i])
            for i, p in enumerate(planes))
        return DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            planes=tuple(planes),
            pixel_format=str(self._metadata.pixel_format),
            dtype=np.dtype(plane_dtype),
            width=w,
            height=h,
            metadata=self._metadata,
            plane_infos=plane_infos,
            extra={
                'source': 'topos-image',
                'image_profile': info.image_profile,
                'color_primaries': info.color_primaries,
                'color_transfer': info.color_transfer,
                'color_matrix': info.color_matrix,
                'color_range': info.color_range,
                'color_authority': self._color_semantics.display_authority.value,
                'ocio_colorspace': self._color_semantics.ocio_colorspace,
                'alpha_mode': info.alpha_mode,
                # 键名对齐视频链（topos_source）：全部消费方按 topos_* 读取
                'topos_alpha_premultiplied': bool(info.alpha_premultiplied),
                'topos_concealed_slices': int(extra.get('concealed_slices', 0)),
                'pixel_aspect': info.pixel_aspect,
                'orientation': info.orientation,
            },
        )

    def seek_frame(self, frame_index: int) -> None:
        if frame_index != 0:
            raise MediaSourceError(f".toos 无帧 {frame_index}", self._path)
        self._current_frame = 0

    def read_audio_samples(self, start_pts: int, count: int) -> Optional[np.ndarray]:
        return None  # 静态图无音频

    # C15（2026-09-27）：对齐 ToposMediaSource 的只读属性契约——
    # frame_provider 等消费方按属性判断（方法形态下 bool(方法) 恒真，
    # 关闭后状态误判）。全仓无 .is_open()/.current_frame() 方法式调用点。
    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def current_frame(self) -> int:
        return self._current_frame

    @property
    def path(self) -> str:
        return self._path

    # —— 附加诊断 ——

    @staticmethod
    def _profile_name(image_profile: int, bit_depth: int = 0) -> str:
        """image_profile 类名后缀（无 TMET 的旧文件兜底；显示时与
        'Topos Image' 族名拼接 → 'Topos Image HQ' 等）。v1.8（ADR-C057）：
        浮点域（profile 4）按样本域命名 → 'Topos Image Float 16bit'
        （HF 命名不直观；产品定名 Low/Medium/High/Ultra × float 词表）。"""
        if image_profile == 4:
            return f'Float {int(bit_depth) or 16}bit'
        return {0: 'Preview', 1: 'HQ', 2: 'XQ',
                3: 'RAW'}.get(image_profile, 'reserved')

    def diagnostics(self) -> dict:
        return {
            'kind': 'topos-image',
            'path': self._path,
            'open': self._open,
        }
