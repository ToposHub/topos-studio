"""色彩元数据配置（中立层）。

原位于 ``src/shared/export/video_encoder.py``：ToposVideoEncoder 是它
在 codec 侧的唯一借用方（默认 SDR / from_metadata），而 PyAV 编码器
同样消费该配置。为让 ``src/shared/codec`` + ``topos_encoder`` 成为
可独立发布的自包含闭环（不反向依赖 PyAV 编码器模块），将定义移入
中立层；``video_encoder`` 保留重导出，现有 import 无需变更。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.shared.media.metadata import VideoMetadata

logger = logging.getLogger(__name__)

# 合法色彩元数据标签（ffmpeg/H.273 词表超集）。类级常量：__post_init__
# 与 from_metadata 的边界映射共用同一份名单（单一真相）。
_VALID_PRIMARIES = (
    "unknown",  # 源素材缺失/不可用色彩元数据时的标记
    "bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m",
    "film", "bt2020", "smpte428", "smpte431", "smpte432", "ebu3213",
)
_VALID_TRANSFER = (
    "unknown",  # 源素材缺失/不可用色彩元数据时的标记
    "bt709", "gamma22", "gamma28", "smpte170m", "smpte240m",
    "linear", "log", "log_sqrt", "iec61966_2_4", "bt1361",
    "iec61966_2_1", "bt2020_10", "bt2020_12", "smpte2084",
    "smpte428", "arib-std-b67",
)
_VALID_MATRIX = (
    "gbr", "bt709", "unknown", "fcc", "bt470bg", "smpte170m",
    "smpte240m", "ycgco", "bt2020nc", "bt2020c", "smpte2085",
    "chroma_nc", "chroma_c", "ictcp",
)
_VALID_RANGE = ("limited", "full", "unknown")


@dataclass
class ColorMetadataConfig:
    """
    色彩元数据配置。

    用于编码时写入色彩信息到视频流。

    Attributes:
        primaries: 色域（'bt709', 'bt2020', 'smpte170m', 'smpte431', 'smpte432'）
        transfer: 传输特性（'bt709', 'smpte2084'(PQ), 'arib-std-b67'(HLG), 'linear'）
        matrix: 色彩矩阵（'bt709', 'bt2020nc', 'smpte170m'）
        range: 色彩范围（'limited', 'full'）
        mastering_display: SMPTE ST 2086 mastering display metadata, as a
            tuple of 8 floats (gx gy bx by rx ry wp_x wp_y) in CIExy; None
            omits the SEI. HDR10 streams should set this.
        max_cll: MaxCLL (content light level) in nits; None omits.
        max_fall: MaxFALL (frame-average light level) in nits; None omits.
    """

    primaries: str = "bt709"
    transfer: str = "bt709"
    matrix: str = "bt709"
    range: str = "limited"
    # Stage-2 HDR static metadata (ST 2086 + CTA-861.3). None = omit SEI.
    mastering_display: Optional[tuple[float, ...]] = None  # (gx,gy,bx,by,rx,ry,wpx,wpy)
    max_cll: Optional[int] = None               # nits
    max_fall: Optional[int] = None              # nits

    def __post_init__(self) -> None:
        """验证色彩元数据。

        AUD-32101 修复：警告与行为保持一致——本层**不改写**字段（不做
        guessed-default masking，与 R-13「未知标签显式失败、不许默认值
        掩盖」一致），无效值原样保留并由下游编码器显式失败
        （topos_encoder._map_color_codes）或跳过（PyAV 映射表未命中）。
        """
        # 仅对真正无效的值发出警告，"unknown" 是合法的缺失标记
        if self.primaries not in _VALID_PRIMARIES:
            logger.warning(
                "Unknown primaries %r kept as-is (no 'bt709' fallback "
                "applied); downstream encoders may reject it",
                self.primaries,
            )
        if self.transfer not in _VALID_TRANSFER:
            logger.warning(
                "Unknown transfer %r kept as-is (no 'bt709' fallback "
                "applied); downstream encoders may reject it",
                self.transfer,
            )
        if self.matrix not in _VALID_MATRIX:
            logger.warning(
                "Unknown matrix %r kept as-is (no 'bt709' fallback "
                "applied); downstream encoders may reject it",
                self.matrix,
            )
        if self.range not in _VALID_RANGE:
            logger.warning(
                "Unknown range %r kept as-is (no 'limited' fallback "
                "applied); downstream encoders may reject it",
                self.range,
            )

    @classmethod
    def from_metadata(cls, metadata: "VideoMetadata") -> "ColorMetadataConfig":
        """
        从 VideoMetadata 创建配置。

        仅使用源素材的实际色彩元数据，None 时用 "unknown" 标记。
        避免错误猜测导致色彩问题（如将 HDR 素材的缺失 primaries 标记为 bt709）。

        Args:
            metadata: 视频元数据

        Returns:
            ColorMetadataConfig 实例
        """
        # 仅使用源素材的实际色彩元数据，None 时用 "unknown"
        # AUD-32101：外部数据（ffmpeg 侧字符串）须验证——名单外的值
        # （拼写变体、新标准名）映射为 "unknown"（本模块的"缺失/不可用"
        # 标记），不再原样下传编码器。
        primaries = cls._from_source(
            metadata.color_primaries, "primaries", metadata.codec_name)
        transfer = cls._from_source(
            metadata.color_transfer, "transfer", metadata.codec_name)
        matrix = cls._from_source(
            metadata.color_matrix, "matrix", metadata.codec_name)
        color_range = cls._from_source(
            metadata.color_range, "range", metadata.codec_name)

        # 当源素材缺少色彩元数据时发出警告
        if primaries == "unknown":
            logger.warning(
                f"Source '{metadata.codec_name}' has no color primaries metadata. "
                f"Output color may be incorrect."
            )

        return cls(primaries=primaries, transfer=transfer, matrix=matrix, range=color_range)

    @staticmethod
    def _from_source(value: Optional[str], kind: str, source: str) -> str:
        """源素材字符串 → 合法标签（None → 'unknown'；名单外 → 'unknown'）。

        名单外的值带告警映射为 "unknown"：值保留在日志中可归因，但不会
        以无效字符串进入编码链（下游对 'unknown' 有显式处理路径）。
        """
        if not value:  # None 或 ""：源素材未声明（与旧 `or "unknown"` 语义一致）
            return "unknown"
        valid = {
            "primaries": _VALID_PRIMARIES,
            "transfer": _VALID_TRANSFER,
            "matrix": _VALID_MATRIX,
            "range": _VALID_RANGE,
        }[kind]
        if value in valid:
            return value
        logger.warning(
            "Source '%s' reports %s=%r outside the known list; "
            "normalized to 'unknown' (output color may be incorrect)",
            source, kind, value,
        )
        return "unknown"

    @classmethod
    def sdr(cls) -> "ColorMetadataConfig":
        """标准 SDR 配置（BT.709）"""
        return cls(primaries="bt709", transfer="bt709", matrix="bt709", range="limited")

    @classmethod
    def hdr10(cls) -> "ColorMetadataConfig":
        """HDR10 配置（BT.2020 + PQ）+ 标准 1000-nit mastering/MaxCLL 元数据。

        Mastering display uses the standard BT.2020/SMPTE RP 431-2 primaries
        with D65 white; MaxCLL/MaxFALL default to 1000 nits (a safe, common
        HDR10 grade target). Override mastering_display/max_cll/max_fall for
        a specific master.
        """
        return cls(
            primaries="bt2020",
            transfer="smpte2084",
            matrix="bt2020nc",
            range="limited",
            # BT.2020 mastering display (gx gy bx by rx ry wpx wpy) + D65 white.
            mastering_display=(0.170, 0.797, 0.131, 0.046,
                               0.708, 0.292, 0.3127, 0.3290),
            max_cll=1000,
            max_fall=180,
        )

    @classmethod
    def hlg(cls) -> "ColorMetadataConfig":
        """HLG 配置（BT.2020 + HLG）"""
        return cls(
            primaries="bt2020",
            transfer="arib-std-b67",
            matrix="bt2020nc",
            range="limited"
        )

    @classmethod
    def dci_p3(cls) -> "ColorMetadataConfig":
        """DCI-P3 配置"""
        return cls(
            primaries="smpte431",
            transfer="smpte2084",
            matrix="bt709",
            range="full"
        )
