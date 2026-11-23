"""阶段 4.4 —— 输入帧色彩描述（YUV 矩阵 / range / 色度采样位置 / transfer / 色域 / 可见区）。

迁移计划 4.4 的要求：把**输入侧**的色彩元数据交给帧描述，而不是散落
在渲染器的实例属性里（``AsyncRenderer.current_video_yuv_matrix`` /
``current_video_color_range`` / ``video_source_size`` / ``visible_rect``
……）。本模块就是这份帧描述：

- 词汇表**复用共享描述**，不另造一套：

  * YUV 矩阵 / range —— ``src.shared.media.gpu_frame`` 的
    ``normalize_color_matrix`` / ``normalize_color_range``（与
    ``PresentColorInfo`` 取值词表一致，``'bt709'`` / ``'bt2020nc'`` /
    ``'smpte170m'`` …，``'limited'`` / ``'full'``）；
  * transfer / 色域 —— ``src.shared.color.types`` 的
    ``ColorTransfer`` / ``ColorPrimaries`` 枚举值（与 scope 面板的
    ``SCOPE_TRANSFERS`` / ``SCOPE_PRIMARIES`` 同源）；
  * 输出边界四元组 —— 直接映射为共享
    ``src.shared.gpu.present_frame.PresentColorInfo``（不建并行结构，
    见 :meth:`ColorFrameDescription.present_color_info`）。

- **与共享描述的映射**（不修改共享侧）：输入侧四元组
  (matrix, range, transfer, primaries) 描述**解码前**的信号；输出侧
  ``PresentColorInfo`` 描述**展示变换后**的可呈现帧。工作空间约定：
  ``working_transfer='linear'``（HDR 不 clamp，>1 的线性值保留）、
  ``working_primaries='bt709'``（与 builtin provider / scope 参考一致；
  ``working_color_context()`` 把该约定交给
  :func:`color_graph_frame_demand_key` 的 ``working_color_context``）。

- **HDR 语义**：transfer ∈ {'pq','hlg'} ⇒ ``is_hdr``；工作空间线性值
  以 SDR 参考白（100 nit）归一——1.0 == 100 nit，>1.0 为高光余量，
  **不得无理由 clamp**（.clinerules「HDR 内部处理」）。

- **可见区**：``visible_rect`` 为纹理坐标行 0 起算的半开区间
  ``(left, top, right, bottom)``，与
  ``NativePresentDescriptor.visible_rect`` 同形；空元组 = 整帧。

纯数据模块：无 Qt / 无 GL / 无 Metal，任何线程可构造可序列化
（:meth:`to_dict` / :meth:`from_dict`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from src.shared.media.gpu_frame import (
    normalize_color_matrix,
    normalize_color_range,
)

# ── 输入词汇表（共享枚举的字符串值，避免 color 域再发明词） ────────────

#: 双平面 YUV 输入格式（VT/解码器侧词汇；NV12=8bit，P010=10bit 左对齐）
PLANAR_YUV_FORMATS: Tuple[str, ...] = ("nv12", "p010")
#: 已支持输入格式
INPUT_FORMATS: Tuple[str, ...] = ("nv12", "p010", "rgba16f")

#: transfer 词汇表（``ColorTransfer`` 枚举值；'scene' 为对数/场景域占位）
FRAME_TRANSFERS: Tuple[str, ...] = (
    "linear", "srgb", "bt709", "pq", "hlg", "scene",
)
#: HDR transfer（工作空间解码后线性值可 >1）
HDR_TRANSFERS: Tuple[str, ...] = ("pq", "hlg")

#: 色域词汇表（``ColorPrimaries`` 枚举值子集 + 'unknown'）
FRAME_PRIMARIES: Tuple[str, ...] = ("bt709", "bt2020", "p3", "unknown")

#: 色度采样位置（FFmpeg ``chroma_sample_location`` 词汇表）
#: left=MPEG-2 共位（NV12 解码默认，与既有 GL ``gid>>1`` 寻址一致）；
#: center=JPEG/ITU-R 601 中位；top*/bottom* 为垂直变体。
CHROMA_LOCATIONS: Tuple[str, ...] = (
    "unknown", "left", "center",
    "topleft", "topright", "bottomleft", "bottomright",
)

#: 工作空间约定（4.4 v1）：线性、SDR 参考白 100 nit 归一、bt709 基色
WORKING_TRANSFER = "linear"
WORKING_PRIMARIES = "bt709"
#: SDR 参考白（nit）——PQ 解码出的绝对亮度按此归一到工作空间
SDR_REFERENCE_NITS = 100.0


def _normalize_transfer(value: Any) -> str:
    key = str(value or "bt709").strip().lower()
    aliases = {
        "709": "bt709", "bt709": "bt709", "bt.709": "bt709", "rec709": "bt709",
        "smpte2084": "pq", "st2084": "pq", "pq": "pq",
        "arib-std-b67": "hlg", "hlg": "hlg",
        "srgb": "srgb", "iecsrgb": "srgb",
        "linear": "linear", "scene-linear": "linear", "scene": "scene",
    }
    transfer = aliases.get(key)
    if transfer is None:
        # 不静默取默认：未知 transfer 会伪装成「正常画面」（.clinerules）
        raise ValueError(f"未知输入 transfer: {value!r}"
                         f"（支持: {', '.join(FRAME_TRANSFERS)}）")
    return transfer


def _normalize_primaries(value: Any) -> str:
    key = str(value or "bt709").strip().lower()
    aliases = {
        "bt709": "bt709", "bt.709": "bt709", "rec709": "bt709", "1": "bt709",
        "bt2020": "bt2020", "bt.2020": "bt2020", "rec2020": "bt2020",
        "9": "bt2020",
        "p3": "p3", "dci-p3": "p3", "display-p3": "p3", "smpte431": "p3",
        "smpte432": "p3", "11": "p3",
        "unknown": "unknown", "": "unknown", "2": "unknown",
    }
    primaries = aliases.get(key)
    if primaries is None:
        raise ValueError(f"未知输入 primaries: {value!r}"
                         f"（支持: {', '.join(FRAME_PRIMARIES)}）")
    return primaries


def _normalize_chroma_location(value: Any) -> str:
    key = str(value or "left").strip().lower().replace("_", "").replace("-", "")
    if key in ("", "unknown", "unspecified"):
        return "left"       # NV12 解码默认（MPEG-2 共位左）
    aliases = {
        "left": "left", "center": "center", "centre": "center",
        "topleft": "topleft", "topright": "topright",
        "bottomleft": "bottomleft", "bottomright": "bottomright",
    }
    location = aliases.get(key)
    if location is None:
        raise ValueError(f"未知色度采样位置: {value!r}"
                         f"（支持: {', '.join(CHROMA_LOCATIONS)}）")
    return location


def _validate_visible_rect(rect: Any, width: int, height: int) -> Tuple[int, ...]:
    if rect is None:
        return ()
    values = tuple(int(v) for v in rect)
    if not values:
        return ()
    if len(values) != 4:
        raise ValueError(f"visible_rect 需为 (l,t,r,b) 四元组: {rect!r}")
    left, top, right, bottom = values
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError(
            f"visible_rect {values} 越界（帧 {width}x{height}，行 0 起算半开区间）")
    return values


@dataclass(frozen=True)
class ColorFrameDescription:
    """一帧输入的色彩/几何描述（解码前信号语义 + 可见区）。

    所有字段已在构造期归一到共享词汇表；尺寸/位深/色度位置与格式
    一致性在 ``__post_init__`` 显式校验（错误立即抛出，不静默修正）。
    """

    pixel_format: str                      # nv12 / p010 / rgba16f
    width: int
    height: int
    bit_depth: int = 8
    yuv_matrix: str = "bt709"              # gpu_frame 词汇表
    yuv_range: str = "limited"             # limited / full
    chroma_location: str = "left"          # CHROMA_LOCATIONS
    transfer: str = "bt709"                # FRAME_TRANSFERS
    primaries: str = "bt709"               # FRAME_PRIMARIES
    #: 纹理坐标行 0 起算的半开区间 (l, t, r, b)；() = 整帧
    visible_rect: Tuple[int, ...] = ()

    def __post_init__(self) -> None:
        fmt = str(self.pixel_format).strip().lower()
        if fmt not in INPUT_FORMATS:
            raise ValueError(f"未知输入格式: {self.pixel_format!r}"
                             f"（支持: {', '.join(INPUT_FORMATS)}）")
        object.__setattr__(self, "pixel_format", fmt)
        if int(self.width) <= 0 or int(self.height) <= 0:
            raise ValueError(f"帧尺寸必须为正: {self.width}x{self.height}")
        object.__setattr__(self, "width", int(self.width))
        object.__setattr__(self, "height", int(self.height))
        expected_depth = 10 if fmt == "p010" else (16 if fmt == "rgba16f" else 8)
        if int(self.bit_depth) != expected_depth:
            raise ValueError(
                f"{fmt} 的位深应为 {expected_depth}，实际 {self.bit_depth}")
        object.__setattr__(self, "bit_depth", int(self.bit_depth))
        if fmt == "rgba16f":
            # RGBA 输入没有 YUV 信号语义；矩阵/range/色度位置不适用
            object.__setattr__(self, "yuv_matrix", "bt709")
            object.__setattr__(self, "yuv_range", "full")
            object.__setattr__(self, "chroma_location", "unknown")
        else:
            object.__setattr__(
                self, "yuv_matrix", normalize_color_matrix(self.yuv_matrix))
            object.__setattr__(
                self, "yuv_range", normalize_color_range(self.yuv_range))
            object.__setattr__(
                self, "chroma_location",
                _normalize_chroma_location(self.chroma_location))
        object.__setattr__(self, "transfer", _normalize_transfer(self.transfer))
        object.__setattr__(
            self, "primaries", _normalize_primaries(self.primaries))
        object.__setattr__(
            self, "visible_rect",
            _validate_visible_rect(self.visible_rect, self.width, self.height))

    # ── 派生语义 ──

    @property
    def is_hdr(self) -> bool:
        """输入是否 HDR（PQ/HLG transfer）。"""
        return self.transfer in HDR_TRANSFERS

    @property
    def is_planar_yuv(self) -> bool:
        """是否双平面 YUV 输入（需要 YUV→RGB 解码级）。"""
        return self.pixel_format in PLANAR_YUV_FORMATS

    @property
    def chroma_size(self) -> Tuple[int, int]:
        """4:2:0 色度平面尺寸（宽高各半，向上取整保护奇数尺寸）。"""
        if not self.is_planar_yuv:
            raise ValueError(f"{self.pixel_format} 无色度平面")
        return ((self.width + 1) // 2, (self.height + 1) // 2)

    # ── 与共享描述的映射 ──

    def present_color_info(self, display_mode: str = "sdr") -> "Any":
        """输出边界四元组 → 共享 ``PresentColorInfo``（不建并行结构）。

        ``display_mode``：展示变换施加后的输出语义——

        * ``'sdr'``：SDR 展示（编码信号直通 / 线性→sRGB 编码后）——
          full-range BT.709（与 ``native_metal_preview`` 的发布四元组
          一致）；
        * ``'sdr_limited'``：SDR 视频信号直通（limited range，YUV 信号
          语义的广播交付面）；
        * ``'hdr_pq'``：线性→PQ 编码的 HDR 展示（BT.2020 基色）；
        * ``'edr_linear'``：EDR 线性直通（extended linear sRGB 层，
          >1 值交由系统合成器映射）。
        """
        from src.shared.gpu.present_frame import PresentColorInfo

        mode = str(display_mode).strip().lower()
        if mode == "sdr":
            return PresentColorInfo(
                color_range="full", color_matrix="bt709",
                color_primaries="bt709", color_transfer="bt709")
        if mode == "sdr_limited":
            return PresentColorInfo(
                color_range="limited", color_matrix="bt709",
                color_primaries="bt709", color_transfer="bt709")
        if mode == "hdr_pq":
            return PresentColorInfo(
                color_range="full", color_matrix="bt2020nc",
                color_primaries="bt2020", color_transfer="pq")
        if mode == "edr_linear":
            return PresentColorInfo(
                color_range="full", color_matrix="bt709",
                color_primaries="bt709", color_transfer="linear")
        raise ValueError(f"未知展示模式: {display_mode!r}")

    def working_color_context(self, working_mode: str = "") -> dict:
        """工作色彩语义（进 ``color_graph_frame_demand_key`` 的上下文键）。

        色彩语义任一变化（如 SDR 码值直通 → HDR 线性工作空间）需求键
        必变，预览/导出/静帧互认同一帧时以该上下文为身份的一部分。
        ``working_mode``：``'signal'``（码值直通，SDR 默认）或
        ``'linear'``（线性，HDR 默认）；缺省按输入自选（见
        ``metal_color_io.default_working_mode``）。
        """
        from src.features.color.core.metal_color_io import (
            default_working_mode,
        )

        mode = working_mode or default_working_mode(self)
        return {
            "working_mode": mode,
            "working_transfer": WORKING_TRANSFER if mode == "linear"
            else "signal",
            "working_primaries": WORKING_PRIMARIES,
            "input_transfer": self.transfer,
            "input_primaries": self.primaries,
            "sdr_reference_nits": SDR_REFERENCE_NITS,
        }

    # ── 序列化 ──

    def to_dict(self) -> dict:
        return {
            "pixel_format": self.pixel_format,
            "width": self.width,
            "height": self.height,
            "bit_depth": self.bit_depth,
            "yuv_matrix": self.yuv_matrix,
            "yuv_range": self.yuv_range,
            "chroma_location": self.chroma_location,
            "transfer": self.transfer,
            "primaries": self.primaries,
            "visible_rect": list(self.visible_rect),
            "is_hdr": self.is_hdr,
        }

    @classmethod
    def from_dict(cls, payload: Optional[Mapping[str, Any]]) -> "ColorFrameDescription":
        data = dict(payload or {})
        unknown = set(data) - {
            "pixel_format", "width", "height", "bit_depth", "yuv_matrix",
            "yuv_range", "chroma_location", "transfer", "primaries",
            "visible_rect", "is_hdr",
        }
        if unknown:
            raise ValueError(f"帧描述含未知字段: {sorted(unknown)}")
        rect = data.get("visible_rect") or ()
        return cls(
            pixel_format=data.get("pixel_format", "rgba16f"),
            width=data.get("width", 0),
            height=data.get("height", 0),
            bit_depth=data.get("bit_depth", 8),
            yuv_matrix=data.get("yuv_matrix", "bt709"),
            yuv_range=data.get("yuv_range", "limited"),
            chroma_location=data.get("chroma_location", "left"),
            transfer=data.get("transfer", "bt709"),
            primaries=data.get("primaries", "bt709"),
            visible_rect=tuple(rect),
        )


@dataclass(frozen=True)
class DisplayOutputDescription:
    """输出边界（展示端）描述：展示变换**只施加一次**的位置与参数。

    由 4.4 的 Metal IO（``metal_color_io``）在输出边界消费；视图侧
    （4.5）按 ``kind`` 决定 ``PresentColorInfo`` 与 EDR 层配置。
    """

    kind: str                               # sdr_passthrough / sdr_encode /
                                            # hdr_sdr_tonemap / hdr_pq / edr_linear
    target_peak_nits: float = SDR_REFERENCE_NITS
    #: EDR 线性直通的高光倍率（>1；屏幕最大 EDR 能力，EDR 不可用时 1.0）
    edr_headroom: float = 1.0

    def __post_init__(self) -> None:
        kind = str(self.kind).strip().lower()
        allowed = ("sdr_passthrough", "sdr_encode", "hdr_sdr_tonemap",
                   "hdr_pq", "edr_linear")
        if kind not in allowed:
            raise ValueError(f"未知展示输出类型: {self.kind!r}"
                             f"（支持: {', '.join(allowed)}）")
        object.__setattr__(self, "kind", kind)
        if float(self.target_peak_nits) <= 0.0:
            raise ValueError("target_peak_nits 必须为正")
        object.__setattr__(self, "target_peak_nits", float(self.target_peak_nits))
        if float(self.edr_headroom) < 1.0:
            raise ValueError("edr_headroom 不得小于 1.0")
        object.__setattr__(self, "edr_headroom", float(self.edr_headroom))

    def present_mode(self) -> str:
        """与 :meth:`ColorFrameDescription.present_color_info` 的模式词对齐。"""
        return {
            "sdr_passthrough": "sdr_limited",
            "sdr_encode": "sdr",
            "hdr_sdr_tonemap": "sdr",
            "hdr_pq": "hdr_pq",
            "edr_linear": "edr_linear",
        }[self.kind]

    def is_hdr_display(self) -> bool:
        return self.kind in ("hdr_pq", "edr_linear")


def display_output_for(description: ColorFrameDescription,
                       *, hdr_display: bool = False,
                       edr_headroom: float = 1.0) -> DisplayOutputDescription:
    """帧描述 + 展示端能力 → 展示输出描述（展示变换施加点唯一）。

    决策表（SDR 输入与 HDR 输入分开，不冒充）：

    * SDR 输入（bt709/srgb 编码信号）：``sdr_passthrough``——既有 GL
      预览即编码信号直通（display_encode 契约 F5.1 的三路语义表）；
      不做「先线性化再编码回去」的往返。
    * HDR 输入 + HDR 展示端：``hdr_pq``（或 EDR 层可用时
      ``edr_linear``——由 ``edr_headroom > 1.0`` 表示能力，调用方按
      屏幕能力选择；本函数在 ``hdr_display and edr_headroom > 1.0``
      时给出 ``edr_linear``）。
    * HDR 输入 + SDR 展示端：``hdr_sdr_tonemap``（ACES 近似
      tonemap + sRGB 编码，与 ``color_transform_provider`` 的
      ``_tonemap_aces_np`` 同源）。
    """
    if description.is_hdr:
        if hdr_display and float(edr_headroom) > 1.0:
            return DisplayOutputDescription(
                "edr_linear", edr_headroom=float(edr_headroom))
        if hdr_display:
            return DisplayOutputDescription("hdr_pq")
        return DisplayOutputDescription("hdr_sdr_tonemap")
    return DisplayOutputDescription("sdr_passthrough")


__all__ = [
    "CHROMA_LOCATIONS",
    "ColorFrameDescription",
    "DisplayOutputDescription",
    "FRAME_PRIMARIES",
    "FRAME_TRANSFERS",
    "HDR_TRANSFERS",
    "INPUT_FORMATS",
    "PLANAR_YUV_FORMATS",
    "SDR_REFERENCE_NITS",
    "WORKING_PRIMARIES",
    "WORKING_TRANSFER",
    "display_output_for",
]
