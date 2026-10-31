"""
Media Metadata Module
定义媒体元数据的统一数据模型。

## 职责

- Rational: 有理数类型，用于精确帧率、时间戳、宽高比表示
- VideoMetadata: 视频流元数据
- AudioMetadata: 音频流元数据（预留）

## 设计原则

- 不可变数据类（frozen=True）确保线程安全
- 完整类型注解
- 不依赖任何外部库（GStreamer/OpenCV/PyAV）
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import gcd
from typing import Optional


@dataclass(frozen=True)
class Rational:
    """
    有理数类型，用于精确表示帧率、时间戳、宽高比。

    采用 numerator/denominator 形式避免浮点精度损失。
    支持常见的帧率格式如 24000/1001 (23.976fps)、30000/1001 (29.97fps)。

    Examples:
        >>> fps_24 = Rational(24, 1)  # 24fps
        >>> fps_23976 = Rational(24000, 1001)  # 23.976fps
        >>> fps_2997 = Rational(30000, 1001)  # 29.97fps
        >>> fps_25 = Rational(25, 1)  # 25fps (PAL)
        >>> fps_50 = Rational(50, 1)  # 50fps
        >>> fps_60 = Rational(60, 1)  # 60fps
    """

    num: int  # 分子 (numerator)
    den: int  # 分母 (denominator)，必须大于 0

    def __post_init__(self) -> None:
        """验证并规范化有理数"""
        if self.den == 0:
            raise ValueError("Denominator cannot be zero")
        # 使用 object.__setattr__ 因为 frozen=True
        if self.den < 0:
            object.__setattr__(self, 'num', -self.num)
            object.__setattr__(self, 'den', -self.den)
        # 约分
        g = gcd(abs(self.num), self.den)
        if g > 1:
            object.__setattr__(self, 'num', self.num // g)
            object.__setattr__(self, 'den', self.den // g)

    def to_float(self) -> float:
        """
        转换为浮点数。

        Returns:
            浮点数值
        """
        return self.num / self.den

    def to_fps(self) -> float:
        """
        转换为帧率浮点数（与 to_float 相同，语义更清晰）。

        Returns:
            帧率浮点数
        """
        return self.to_float()

    def frame_to_pts(self, frame: int) -> int:
        """
        将帧号转换为时间戳（PTS，Presentation Time Stamp）。

        PTS 单位取决于时间基（time_base），这里使用帧率倒数作为时间基。
        例如：24fps 视频的第 100 帧 PTS = 100 * 1 = 100

        Args:
            frame: 帧号（从 0 开始）

        Returns:
            PTS 值（整数时间戳）
        """
        return frame * self.den

    def pts_to_frame(self, pts: int) -> int:
        """
        将时间戳（PTS）转换为帧号。

        PTS 时间基为帧率倒数（den/num）秒，单位为 den。
        即每帧 PTS 间隔 = den。

        Args:
            pts: PTS 值（整数时间戳）

        Returns:
            帧号（向下取整）
        """
        if pts < 0:
            return 0
        return pts // self.den

    def frame_to_seconds(self, frame: int) -> float:
        """
        将帧号转换为秒数。

        Args:
            frame: 帧号（从 0 开始）

        Returns:
            秒数（浮点数）
        """
        return frame / self.to_fps()

    def seconds_to_frame(self, seconds: float) -> int:
        """
        将秒数转换为帧号。

        Args:
            seconds: 秒数

        Returns:
            帧号（向下取整）
        """
        return int(seconds * self.to_fps())

    def __str__(self) -> str:
        """字符串表示"""
        return f"{self.num}/{self.den}"

    def __repr__(self) -> str:
        """详细表示"""
        return f"Rational({self.num}, {self.den})"

    def __float__(self) -> float:
        """支持 float() 转换"""
        return self.to_float()

    def __mul__(self, other: int | float | Rational) -> Rational:
        """乘法运算"""
        if isinstance(other, int):
            return Rational(self.num * other, self.den)
        elif isinstance(other, float):
            # 浮点数乘法，转为有理数近似
            return Rational(int(self.num * other * 1000), self.den * 1000)
        elif isinstance(other, Rational):
            return Rational(self.num * other.num, self.den * other.den)
        return NotImplemented

    def __truediv__(self, other: int | float | Rational) -> Rational:
        """除法运算"""
        if isinstance(other, int):
            # 除以整数：分子不变，分母乘以整数
            return Rational(self.num, self.den * other)
        elif isinstance(other, float):
            # 除以浮点数
            frac = Fraction(1.0 / other).limit_denominator(10000)
            return Rational(self.num * frac.numerator, self.den * frac.denominator)
        elif isinstance(other, Rational):
            return Rational(self.num * other.den, self.den * other.num)
        return NotImplemented

    @staticmethod
    def from_float(value: float, max_den: int = 10000) -> Rational:
        """
        从浮点数创建有理数（使用连分数近似）。

        Args:
            value: 浮点数值
            max_den: 最大分母限制

        Returns:
            近似的 Rational
        """
        from fractions import Fraction
        frac = Fraction(value).limit_denominator(max_den)
        return Rational(frac.numerator, frac.denominator)

    @staticmethod
    def from_string(s: str) -> Rational:
        """
        从字符串解析有理数。

        支持格式：
        - "24/1" -> Rational(24, 1)
        - "30000/1001" -> Rational(30000, 1001)
        - "23.976" -> Rational(24000, 1001) (近似)

        外部字符串（容器/时间线 JSON）不做静默截断：分段数不是 2 或
        分子/分母不是整数时抛带上下文的 ValueError（AUD-35400）。

        Args:
            s: 字符串

        Returns:
            Rational 实例

        Raises:
            ValueError: 字符串不是合法的有理数表示
        """
        text = s.strip() if isinstance(s, str) else s
        if not isinstance(text, str):
            raise ValueError(f"invalid rational string: {s!r}")
        if '/' in text:
            parts = text.split('/')
            if len(parts) != 2:
                # AUD-35400：此前多余分段被静默丢弃（"24/1/1" 被当作
                # 24/1），错误数据无痕通过；现在显式拒绝。
                raise ValueError(
                    f"invalid rational string (expected num/den): {text!r}"
                )
            try:
                num = int(parts[0].strip())
                den = int(parts[1].strip())
            except ValueError as exc:
                raise ValueError(f"invalid rational string: {text!r}") from exc
            return Rational(num, den)
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f"invalid rational string: {text!r}") from exc
        return Rational.from_float(value)


@dataclass(frozen=True)
class VideoMetadata:
    """
    视频流元数据。

    包含视频的所有技术参数，用于解码、渲染和色彩管理。

    Attributes:
        width: 视频宽度（像素）
        height: 视频高度（像素）
        fps: 帧率（有理数表示）
        total_frames: 总帧数，可能为 None（流媒体）
        codec_name: 编码器名称（如 'h264', 'hevc', 'prores'）
        codec_profile: 编码器配置（如 'High', 'Main', '422'）
        pixel_format: 像素格式（如 'yuv420p', 'yuv422p10le', 'rgb48le'）
        bit_depth: 位深度（8, 10, 12, 16）
        color_primaries: 色域（'bt709', 'bt2020', 'smpte170m'）
        color_transfer: 传输特性（'bt709', 'smpte2084'(PQ), 'arib-std-b67'(HLG)）
        color_matrix: 色彩矩阵（'bt709', 'bt2020nc'）
        color_range: 色彩范围（'limited', 'full'）
        chroma_siting: 色度采样位置（0=left, 1=center, 2=topleft），未知时为 None
        sample_aspect_ratio: 像素宽高比（SAR）
        timecode: 起始时间码
        has_alpha: 是否包含 Alpha 通道
        field_order: 场序（'progressive', 'top', 'bottom'）
        rotation: 旋转角度（0, 90, 180, 270）
        timebase: 容器时间基（每 tick 的秒数），可选
        duration_ticks: 容器时间基中的精确总时长，可选
    """

    width: int
    height: int
    fps: Rational
    total_frames: Optional[int]
    codec_name: str
    codec_profile: Optional[str]
    pixel_format: str
    bit_depth: int
    color_primaries: Optional[str] = None
    color_transfer: Optional[str] = None
    color_matrix: Optional[str] = None
    color_range: Optional[str] = None
    sample_aspect_ratio: Optional[Rational] = None
    timecode: Optional[str] = None
    # v1.5 tmcd 起始时码语义（container_spec_v1.5；仅 topos 源给出）。
    # timecode 字符串即起始时码 HH:MM:SS[;:]FF；fps/DF 与之同源，
    # 无 tmcd 轨时三者同 None。
    timecode_fps: Optional[int] = None
    timecode_drop_frame: Optional[bool] = None
    has_alpha: bool = False
    field_order: Optional[str] = None
    rotation: int = 0
    timebase: Optional[Rational] = None
    duration_ticks: Optional[int] = None
    # Topos chroma-location enum: 0=left, 1=center, 2=topleft. None means
    # the source/container does not expose a location that can be trusted.
    chroma_siting: Optional[int] = None
    # v1.1 音频轨（container_spec_v1.1；仅 topos/ffmpeg 源可给出）。
    # has_audio=False 时其余音频字段为 None。
    has_audio: bool = False
    audio_channels: Optional[int] = None
    audio_sample_rate: Optional[int] = None
    audio_codec: Optional[str] = None  # 'pcm_s16be'/'pcm_s24be'/'pcm_s32be'/'aac'
    # v1.6 多音轨（M-B8；仅 topos 源给出）：逐轨探测元数据，每项
    # {name, codec, sample_rate, channel_count, channel_layout,
    #  bits_per_sample, sample_format, priming_samples}。旧 audio_* 字段
    # 保持为首轨视图（轨 0），兼容既有消费方；无音轨为空元组。
    audio_tracks: tuple = ()

    def __post_init__(self) -> None:
        """验证元数据有效性"""
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Invalid dimensions: {self.width}x{self.height}")
        if self.bit_depth not in (8, 10, 12, 14, 16, 32):
            raise ValueError(f"Invalid bit depth: {self.bit_depth}")
        if self.rotation not in (0, 90, 180, 270):
            raise ValueError(f"Invalid rotation: {self.rotation}")
        # AUD-35401：fps 与 SAR 必须为正。此前不校验，fps.num==0 时
        # duration_seconds 抛 ZeroDivisionError、SAR==0 时 display_size
        # 抛 ZeroDivisionError；在构造点 fail-loud 比在使用点崩更早更
        # 明确（所有生产源在构造前已保证正值，见各 _extract_* 实现）。
        if self.fps.num <= 0:
            raise ValueError(f"fps must be positive: {self.fps}")
        if self.sample_aspect_ratio is not None and self.sample_aspect_ratio.num <= 0:
            raise ValueError(
                f"sample_aspect_ratio must be positive: {self.sample_aspect_ratio}"
            )
        if self.timebase is not None and self.timebase.num <= 0:
            raise ValueError("timebase must be positive")
        if self.duration_ticks is not None and self.duration_ticks < 0:
            raise ValueError("duration_ticks must be non-negative")

    @property
    def duration_seconds(self) -> Optional[float]:
        """
        计算视频时长（秒）。

        Returns:
            时长秒数，如果 total_frames 为 None 则返回 None
        """
        if self.duration_ticks is not None and self.timebase is not None:
            return self.duration_ticks * self.timebase.to_float()
        if self.total_frames is None:
            return None
        return self.total_frames / self.fps.to_fps()

    @property
    def display_size(self) -> tuple[int, int]:
        """
        获取显示尺寸（考虑 SAR）。

        Returns:
            (display_width, display_height)
        """
        if self.sample_aspect_ratio is None:
            return (self.width, self.height)

        sar = self.sample_aspect_ratio.to_float()
        if sar == 1.0:
            return (self.width, self.height)

        if sar > 1.0:
            # 非方形像素，水平拉伸
            return (int(self.width * sar), self.height)
        else:
            # 非方形像素，垂直拉伸
            return (self.width, int(self.height / sar))

    @property
    def is_hdr(self) -> bool:
        """
        判断是否为 HDR 视频。

        Returns:
            是否为 HDR（PQ 或 HLG 传输特性）
        """
        if self.color_transfer is None:
            return False
        return self.color_transfer in ('smpte2084', 'arib-std-b67')

    @property
    def is_interlaced(self) -> bool:
        """
        判断是否为隔行扫描。

        Returns:
            是否为隔行扫描
        """
        if self.field_order is None:
            return False
        return self.field_order != 'progressive'


# 编码族显示名：codec_name（FFmpeg 小写名/自研名）→ 面板展示名
_CODEC_FAMILY_DISPLAY: dict[str, str] = {
    'topos': 'Topos',
    'prores': 'ProRes',
    'topos-image': 'Topos Image',
    'topos-image-sequence': 'Topos Image Sequence',
}

# profile 已自带这些族前缀时原样显示（不重复拼接族名）——覆盖 topos 源
# profile（'Topos 422 HQ'）、图片 TMET label（'toos High 12bit'）等
_KNOWN_PROFILE_PREFIXES: tuple[str, ...] = ('topos', 'prores', 'toos')

# FFmpeg ProRes profile 名 → 档位后缀（对齐导出面板 "ProRes 422 HQ" 口径；
# FFmpeg 的 profile 名不含 "422"，Standard 即 422 基准档）
_PRORES_PROFILE_DISPLAY: dict[str, str] = {
    'proxy': '422 Proxy',
    'lt': '422 LT',
    'standard': '422',
    'hq': '422 HQ',
    '4444': '4444',
    '4444 xq': '4444 XQ',
}


def format_codec_label(codec_name: Optional[str], codec_profile: Optional[str]) -> str:
    """codec_name + codec_profile → 面板显示标签（如 'ProRes 422 HQ'、'Topos 422'）。

    - 两部分都缺失 → 返回空串（调用方自行决定占位符）。
    - ProRes：FFmpeg profile 名映射为 422/4444 家族档位后缀。
    - profile 已含已知族前缀（'Topos 422'、'toos High 12bit'）或本族名
      → 原样显示，不重复拼接。
    - 其余编码（如 h264 'High'）→ '族名 profile' 拼接；profile 缺失 → 仅族名。
    """
    family_raw = (codec_name or '').strip()
    profile_raw = (codec_profile or '').strip()
    if not family_raw and not profile_raw:
        return ''
    family = _CODEC_FAMILY_DISPLAY.get(family_raw.lower(), family_raw)
    if family_raw.lower() == 'prores':
        suffix = _PRORES_PROFILE_DISPLAY.get(profile_raw.lower())
        if suffix:
            return f'{family} {suffix}'.strip()
    lowered = profile_raw.lower()
    # 旧缓存兼容：图片资产 profile 曾以 'Image HQ' 形式缓存——与族名
    # 'Topos Image' 拼接会重复，剥掉 'Image ' 前缀再拼。
    if lowered.startswith('image ') and family.lower().endswith('image'):
        profile_raw = profile_raw[len('Image '):]
        lowered = lowered[len('image '):]
    if profile_raw and (lowered.startswith(_KNOWN_PROFILE_PREFIXES)
                        or lowered.startswith(family.lower())):
        return profile_raw
    return f'{family} {profile_raw}'.strip() if profile_raw else family


# 压缩音频编码格式列表
COMPRESSED_AUDIO_CODECS: frozenset[str] = frozenset({
    # 有损压缩
    'mp3', 'mp2', 'mp1',           # MPEG Audio
    'aac', 'aac_latm', 'aac_lc',   # AAC
    'ac3', 'eac3', 'truehd',       # Dolby
    'dts', 'dts_hd', 'dts_hd_ma',  # DTS
    'vorbis', 'opus',              # 开源编码
    'wma', 'wmapro',               # Windows Media
    'flac',                        # FLAC（无损压缩）
    'alac',                        # Apple Lossless
    'ape',                         # Monkey's Audio
    'wavpack',                     # WavPack
})

# 无压缩音频编码格式列表
UNCOMPRESSED_AUDIO_CODECS: frozenset[str] = frozenset({
    'pcm_s16le', 'pcm_s16be',
    'pcm_s24le', 'pcm_s24be',
    'pcm_s32le', 'pcm_s32be',
    'pcm_f32le', 'pcm_f32be',
    'pcm_f64le', 'pcm_f64be',
    'pcm_u8', 'pcm_s8',
    'pcm_s24daud',
    'pcm_dvd',
    'pcm_bluray',
})


@dataclass(frozen=True)
class AudioMetadata:
    """
    音频流元数据。

    包含音频的所有技术参数，用于音频处理和同步。

    Attributes:
        sample_rate: 采样率（Hz），常见值：48000, 44100, 96000
        channels: 声道数，常见值：1(mono), 2(stereo), 6(5.1), 8(7.1)
        channel_layout: 声道布局（'mono', 'stereo', '5.1', '7.1'）
        duration_seconds: 时长（秒）
        start_pts: 起始时间戳（PTS）
        codec_name: 编码器名称（'pcm_s16le', 'aac', 'mp3'）
        bit_depth: 位深度（16, 24, 32），压缩格式可能为 None
    """

    sample_rate: int
    channels: int
    channel_layout: Optional[str] = None
    duration_seconds: Optional[float] = None
    start_pts: Optional[int] = None
    codec_name: Optional[str] = None
    bit_depth: Optional[int] = None

    def __post_init__(self) -> None:
        """验证音频元数据有效性"""
        if self.sample_rate <= 0:
            raise ValueError(f"Invalid sample rate: {self.sample_rate}")
        if self.channels <= 0:
            raise ValueError(f"Invalid channel count: {self.channels}")
        if self.bit_depth is not None and self.bit_depth not in (8, 16, 24, 32, 64):
            raise ValueError(f"Invalid bit depth: {self.bit_depth}")

    @property
    def total_samples(self) -> Optional[int]:
        """
        计算总采样数。

        Returns:
            总采样数，如果 duration_seconds 为 None 则返回 None
        """
        if self.duration_seconds is None:
            return None
        return int(self.duration_seconds * self.sample_rate)

    @property
    def is_compressed(self) -> bool:
        """
        判断是否为压缩格式。

        Returns:
            是否为压缩格式（有损或无损压缩）
        """
        if self.codec_name is None:
            return False
        codec_lower = self.codec_name.lower()
        return codec_lower in COMPRESSED_AUDIO_CODECS

    @property
    def is_lossless(self) -> bool:
        """
        判断是否为无损格式。

        Returns:
            是否为无损压缩格式（FLAC, ALAC 等）
        """
        if self.codec_name is None:
            return False
        codec_lower = self.codec_name.lower()
        return codec_lower in {'flac', 'alac', 'ape', 'wavpack'}

    @property
    def is_pcm(self) -> bool:
        """
        判断是否为 PCM 无压缩格式。

        Returns:
            是否为 PCM 格式
        """
        if self.codec_name is None:
            return False
        codec_lower = self.codec_name.lower()
        return codec_lower in UNCOMPRESSED_AUDIO_CODECS

    @property
    def bitrate(self) -> Optional[int]:
        """
        计算比特率（bps）。

        对于 PCM 格式，返回原始比特率。
        对于压缩格式，返回 None（需要从流信息获取）。

        Returns:
            比特率（bits per second），无法计算则返回 None
        """
        if self.bit_depth is None:
            return None
        if not self.is_pcm:
            return None
        return self.sample_rate * self.channels * self.bit_depth

    @property
    def bytes_per_sample(self) -> int:
        """
        获取每样本字节数。

        Returns:
            每样本字节数（所有声道总和）
        """
        if self.bit_depth is None:
            return 2 * self.channels
        return (self.bit_depth // 8) * self.channels


# 预定义的常见帧率
FPS_23_976 = Rational(24000, 1001)  # 电影标准（24/1.001）
FPS_24 = Rational(24, 1)             # 电影
FPS_25 = Rational(25, 1)             # PAL 电视
FPS_29_97 = Rational(30000, 1001)   # NTSC 电视
FPS_30 = Rational(30, 1)             # 常见帧率
FPS_50 = Rational(50, 1)             # PAL 逐行
FPS_59_94 = Rational(60000, 1001)   # NTSC 逐行
FPS_60 = Rational(60, 1)             # 高帧率

# 常见帧率列表
COMMON_FRAME_RATES: list[Rational] = [
    FPS_23_976,
    FPS_24,
    FPS_25,
    FPS_29_97,
    FPS_30,
    FPS_50,
    FPS_59_94,
    FPS_60,
]
